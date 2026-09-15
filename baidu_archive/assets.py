"""资源下载与正文本地化。

铁律：
* ``content.original.html`` 与 ``raw/`` 永不修改。
* 下载失败**不删除**任何 HTML 元素 —— 失效的 URL 本身就是档案信息。
* 只有 http(s) 才下载；``data:`` / ``javascript:`` 等正确分类而不是当 HTTP 处理。
"""

from __future__ import annotations

import html as html_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import httpx

from .client import ArchiveClient, AuthExpired
from .config import Config
from .parsers import make_soup
from .state import ArchiveState
from .storage import read_text, write_json_atomic, write_text_atomic, write_atomic
from .utils import extension_for, redact_url, sha256_bytes, utcnow_iso

Logger = Callable[[str, str], None]


def _noop(tag: str, message: str) -> None:  # pragma: no cover
    print(f"[{tag}] {message}")


def _asset_filename(index: int, digest: str, extension: str) -> str:
    """``0001_<sha12><ext>``：序号保留正文中的出现顺序，sha 保证不同内容不互相覆盖。"""
    return f"{index:04d}_{digest[:12]}{extension}"


def _download_one(
    client: ArchiveClient,
    config: Config,
    asset: dict[str, Any],
    assets_dir: Path,
    index: int,
) -> dict[str, Any]:
    """下载单个资源。返回写回 state 的字段字典。"""
    url = asset.get("resolved_url")
    result: dict[str, Any] = {"download_attempted": 1}
    try:
        client.limiter.wait()
        with client._client.stream("GET", url, timeout=config.asset_timeout) as response:
            result["http_status"] = response.status_code
            content_type = response.headers.get("content-type", "")
            result["content_type"] = content_type
            if response.status_code != 200:
                result["download_status"] = "failed"
                result["error"] = f"HTTP {response.status_code}"
                return result

            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > config.max_asset_bytes:
                    result["download_status"] = "failed"
                    result["error"] = f"超过大小上限 {config.max_asset_bytes} 字节"
                    return result
                chunks.append(chunk)
            body = b"".join(chunks)

        if not body:
            result["download_status"] = "failed"
            result["error"] = "响应为空"
            return result

        digest = sha256_bytes(body)
        extension = extension_for(content_type, url)
        filename = _asset_filename(index, digest, extension)
        target = assets_dir / filename

        # 同名同内容 -> 直接复用；同名不同内容不可能发生（文件名含 sha 前缀）
        if not target.exists():
            write_atomic(target, body)

        result.update(
            {
                "download_status": "ok",
                "local_path": f"assets/{filename}",
                "sha256": digest,
                "size": len(body),
                "error": None,
            }
        )
        return result
    except httpx.HTTPError as exc:
        result["download_status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"[:400]
        return result
    except OSError as exc:
        result["download_status"] = "failed"
        result["error"] = f"写入失败: {exc}"[:400]
        return result


def localize_html(content_html: str, mapping: dict[str, str]) -> str:
    """生成 ``content.local.html``：把已下载成功的 src 重写成本地相对路径。

    ``mapping`` 为 ``{原始URL: 本地相对路径}``。未命中的元素**保持原样**，
    并额外记录 ``data-archive-original-src`` 以便日后追溯。
    """
    if not content_html:
        return content_html
    soup = make_soup(content_html)

    for node in soup.find_all(["img", "video", "audio", "source", "embed"]):
        for attr in ("src", "data-src", "data-original", "data-lazy-src"):
            raw = node.get(attr)
            if not raw:
                continue
            candidate = html_module.unescape(str(raw).strip())
            local = mapping.get(candidate)
            if local:
                if attr == "src" or not node.get("src"):
                    node["data-archive-original-src"] = candidate
                    node["src"] = local
                    # 懒加载属性已失去意义，但保留原值不删，仅标记
                    break
    for node in soup.find_all("object", data=True):
        candidate = html_module.unescape(str(node["data"]).strip())
        local = mapping.get(candidate)
        if local:
            node["data-archive-original-data"] = candidate
            node["data"] = local

    # srcset 一旦本地化会指向不存在的多分辨率文件，直接移除但保留原值备查
    for node in soup.find_all("img", srcset=True):
        if node.get("data-archive-original-src"):
            node["data-archive-original-srcset"] = node["srcset"]
            del node["srcset"]

    # 正文是 HTML 片段而非完整文档；lxml 会补出 <html><body>，这里只取回片段本身
    if soup.body is not None:
        return soup.body.decode_contents()
    if soup.html is not None:
        return soup.html.decode_contents()
    return str(soup)


def run_assets(
    config: Config,
    state: ArchiveState,
    client: ArchiveClient,
    *,
    log: Logger = _noop,
    force: bool = False,
    limit: int | None = None,
    include_media: bool | None = None,
) -> dict[str, Any]:
    """下载资源并生成 content.local.html + asset_manifest.json。"""
    config.ensure_dirs()
    want_media = config.download_other_media if include_media is None else include_media

    records = state.iter_records("page")
    if limit:
        records = records[:limit]

    totals = {"items": 0, "discovered": 0, "attempted": 0, "downloaded": 0, "failed": 0, "skipped_non_http": 0}
    aborted: str | None = None

    for record in records:
        key = record["key"]
        item = state.get_item(key) or {}
        if item.get("status") not in {"parsed", "warning", "assets_partial", "complete"}:
            continue
        if item.get("status") == "complete" and not force:
            continue

        item_dir = config.item_dir(key)
        original_path = item_dir / "content.original.html"
        if not original_path.exists():
            continue
        content_html = read_text(original_path) or ""

        assets = state.assets_for(key)
        totals["items"] += 1
        totals["discovered"] += len(assets)

        assets_dir = item_dir / "assets"
        downloadable = []
        for position, asset in enumerate(assets, start=1):
            if not asset.get("downloadable") or not asset.get("resolved_url"):
                totals["skipped_non_http"] += 1
                continue
            kind = asset.get("kind")
            if kind == "image" and not config.download_images:
                continue
            if kind == "link":
                continue  # 外链只登记，绝不递归抓取外站
            if kind != "image" and not want_media:
                continue
            if asset.get("download_status") == "ok" and not force:
                continue
            downloadable.append((position, asset))

        if downloadable:
            assets_dir.mkdir(parents=True, exist_ok=True)
            with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as pool:
                futures = {
                    pool.submit(_download_one, client, config, asset, assets_dir, position): asset
                    for position, asset in downloadable
                }
                for future in as_completed(futures):
                    asset = futures[future]
                    try:
                        outcome = future.result()
                    except AuthExpired as exc:
                        aborted = str(exc)
                        break
                    except Exception as exc:  # noqa: BLE001
                        outcome = {
                            "download_attempted": 1,
                            "download_status": "failed",
                            "error": f"{type(exc).__name__}: {exc}"[:400],
                        }
                    totals["attempted"] += 1
                    if outcome.get("download_status") == "ok":
                        totals["downloaded"] += 1
                    else:
                        totals["failed"] += 1
                    state.record_asset_result(
                        key, asset["original_url"], asset["attribute"], **outcome
                    )
        if aborted:
            break

        # ---- 重写本地版 + manifest ----
        refreshed = state.assets_for(key)
        mapping = {
            a["original_url"]: a["local_path"]
            for a in refreshed
            if a.get("download_status") == "ok" and a.get("local_path")
        }
        # resolved_url 与 original_url 不同时（相对路径），两个键都要能命中
        for a in refreshed:
            if a.get("download_status") == "ok" and a.get("local_path") and a.get("resolved_url"):
                mapping.setdefault(a["resolved_url"], a["local_path"])

        write_text_atomic(item_dir / "content.local.html", localize_html(content_html, mapping))

        manifest = []
        for a in refreshed:
            manifest.append(
                {
                    "original_url": a["original_url"],
                    "resolved_url": a["resolved_url"],
                    "redacted_url": redact_url(a["resolved_url"] or a["original_url"] or ""),
                    "kind": a["kind"],
                    "element_type": a["element_type"],
                    "attribute": a["attribute"],
                    "scheme_kind": a["scheme_kind"],
                    "domain": a["domain"],
                    "download_attempted": bool(a["download_attempted"]),
                    "download_status": a["download_status"],
                    "http_status": a["http_status"],
                    "content_type": a["content_type"],
                    "local_path": a["local_path"],
                    "sha256": a["sha256"],
                    "size": a["size"],
                    "error": a["error"],
                }
            )
        write_json_atomic(item_dir / "asset_manifest.json", {
            "key": key,
            "generated_at": utcnow_iso(),
            "count": len(manifest),
            "downloaded": sum(1 for m in manifest if m["download_status"] == "ok"),
            "assets": manifest,
        })

        # 同步进 metadata.json
        metadata_path = item_dir / "metadata.json"
        if metadata_path.exists():
            import json as _json

            try:
                metadata = _json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError):
                metadata = None
            if metadata is not None:
                metadata["assets"] = manifest
                metadata["asset_count_downloaded"] = sum(1 for m in manifest if m["download_status"] == "ok")
                write_json_atomic(metadata_path, metadata)

        expected = [m for m in manifest if m["download_attempted"]]
        failed_here = [m for m in expected if m["download_status"] != "ok"]
        if failed_here:
            state.update_item(key, status="assets_partial", assets_done_at=utcnow_iso())
            log("ASSET", f"key={key} {len(expected)-len(failed_here)}/{len(expected)} 下载成功（部分失败）")
        else:
            state.advance_status(key, "complete", assets_done_at=utcnow_iso())
            if expected:
                log("ASSET", f"key={key} {len(expected)}/{len(expected)} 全部下载成功")

    if aborted:
        log("AUTH", aborted)
    totals["aborted"] = aborted
    log("ASSET", f"完成：发现 {totals['discovered']}，尝试 {totals['attempted']}，"
                 f"成功 {totals['downloaded']}，失败 {totals['failed']}")
    return totals
