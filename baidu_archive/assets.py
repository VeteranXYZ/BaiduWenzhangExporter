"""资源下载与正文本地化。

铁律：
* ``content.original.html`` 与 ``raw/`` 永不修改。
* 下载失败**不删除**任何 HTML 元素 —— 失效的 URL 本身就是档案信息。
* 只有 http(s) 才下载；``data:`` / ``javascript:`` 等正确分类而不是当 HTTP 处理。
"""

from __future__ import annotations

import html as html_module
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

import httpx

from .client import ArchiveClient, AuthExpired
from .config import Config
from .media import image_extension, validate_media_response
from .truth import article_truth, index_truth, resource_fingerprint, resource_identity, validate_downloaded_asset
from uuid import uuid4
from .parsers import extract_assets, make_soup, parse_srcset
from .state import ArchiveState
from .tasks import TaskControl, TaskStopped, bounded_results
from .storage import IntegrityError, current_content_path, read_json, read_text, save_raw, write_json_atomic, write_text_atomic, write_once
from .utils import redact_url, safe_key_dirname, sha256_bytes, sha256_file, utcnow_iso

Logger = Callable[[str, str], None]
LOCALIZATION_VERSION = 2


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

        digest = sha256_bytes(body)
        validation = validate_media_response(asset.get('kind'), body, content_type)
        extension = validation['extension']
        if validation['status'] != 'accepted':
            rejected = config.raw_dir / "asset_responses" / safe_key_dirname(asset["key"]) / f"{digest}.bin"
            save_raw(rejected, body, url=url, status_code=result["http_status"],
                     headers={"content-type": content_type}, extra={"stage": "asset_rejected", 'validation': validation})
            result.update(download_status="failed", error=validation['status'] + ': ' + validation['reason'])
            return result
        filename = _asset_filename(index, digest, extension)
        target = assets_dir / filename

        # Preserve a corrupt preexisting file in place. A fresh version gets a new
        # name rather than silently overwriting evidence or failing recovery.
        if (target.exists() or target.is_symlink()) and (target.is_symlink() or not target.is_file() or target.read_bytes() != body):
            filename = f'{index:04d}_{digest[:12]}_{uuid4().hex}{extension}'
            target = assets_dir / filename
        write_once(target, body)

        result.update(
            {
                "download_status": "ok",
                "local_path": f"assets/{filename}",
                "sha256": digest,
                "size": len(body),
                "error": None,
                'download_identity': resource_identity(asset),
            }
        )
        return result
    except httpx.HTTPError as exc:
        result["download_status"] = "failed"
        result["error"] = type(exc).__name__
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
        # 显式 lazy 地址优先于 src（src 常是 HTTP 或 data URI 占位图）。
        chosen_lazy = False
        for attr in ("data-src", "data-original", "data-lazy-src", "src"):
            raw = node.get(attr)
            if not raw:
                continue
            candidate = html_module.unescape(str(raw).strip())
            local = mapping.get(candidate)
            if local:
                node["data-archive-original-src"] = candidate
                node["src"] = local
                chosen_lazy = attr != 'src'
                break
        if node.name == 'img' or (node.name == 'source' and node.find_parent('picture')):
            raw_set = str(node.get('srcset') or '')
            if not raw_set:
                continue
            candidates = parse_srcset(raw_set)
            localized = [(mapping[url], descriptor) for url, descriptor in candidates if url in mapping]
            # data URI 可直接离线使用；不把远程失败候选混入成功的本地集合。
            offline = [(mapping.get(url, url), descriptor) for url, descriptor in candidates
                       if url in mapping or url.startswith('data:image/')]
            if chosen_lazy or localized or node.get('data-archive-original-src'):
                node['data-archive-original-srcset'] = raw_set
                if offline and not chosen_lazy:
                    node['srcset'] = ', '.join(url + (' ' + desc if desc else '') for url, desc in offline)
                    if node.name == 'img' and not node.get('data-archive-original-src'):
                        node['src'] = localized[0][0]
                        node['data-archive-original-src'] = next(url for url, _ in candidates if url in mapping)
                else:
                    del node['srcset']
    for node in soup.find_all("object", data=True):
        candidate = html_module.unescape(str(node["data"]).strip())
        local = mapping.get(candidate)
        if local:
            node["data-archive-original-data"] = candidate
            node["data"] = local

    # 失败的 picture source 不能覆盖已经成功本地化的 img fallback。
    for node in soup.select('picture source[srcset]'):
        fallback = node.find_parent('picture').find('img')
        if fallback and fallback.get('data-archive-original-src') and not node.get('data-archive-original-srcset'):
            node['data-archive-original-srcset'] = node['srcset']
            del node['srcset']

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
    only_keys: list[str] | None = None,
) -> dict[str, Any]:
    """下载资源并生成 content.local.html + asset_manifest.json。"""
    config.ensure_dirs()
    want_media = config.download_other_media if include_media is None else include_media

    records = state.iter_records("page")
    if only_keys is not None:
        wanted = set(only_keys)
        records = [record for record in records if record["key"] in wanted]
    if limit:
        records = records[:limit]

    totals = {"items": 0, "discovered": 0, "attempted": 0, "downloaded": 0, "failed": 0, "skipped_non_http": 0}
    aborted: str | None = None
    control = getattr(client, "control", None) or TaskControl()

    for record in records:
        control.check()
        key = record["key"]
        item = state.get_item(key) or {}
        if item.get("status") not in {"parsed", "warning", "assets_partial", "complete"}:
            continue
        item_dir = config.item_dir(key)
        invalid_assets = []
        for asset in state.assets_for(key):
            if asset.get('download_status') == 'ok':
                validation = validate_downloaded_asset(config, asset)
                if not validation['ok']:
                    invalid_assets.append((asset, validation['reason']))
        for asset, reason in invalid_assets:
            state.invalidate_asset(asset, reason)
        if invalid_assets:
            state.update_item(key, status="assets_partial")
            item["status"] = "assets_partial"
        facts = article_truth(config, state, key)
        if any(reason.startswith(('stale_', 'invalid_content_snapshot', 'invalid_detail_snapshot')) for reason in facts['issues']):
            state.update_item(key, status='parsed', assets_done_at=None)
            raise IntegrityError('当前 raw 世代尚未重新解析，请运行 parse')
        original_path = current_content_path(item_dir, prefer_local=False)
        if not original_path.exists():
            totals['failed'] += 1
            state.update_item(key, status='warning', warning='当前正文缺失')
            continue
        content_html = read_text(original_path) or ""

        assets = state.assets_for(key)
        metadata = read_json(item_dir / 'metadata.json', {}) or {}
        fingerprint = resource_fingerprint(metadata.get('parsed_sha256'), metadata.get('iframe_url_original'), assets)
        if metadata.get('original_sha256') and sha256_file(item_dir / 'content.original.html') != metadata['original_sha256']:
            state.update_item(key, status='warning')
            raise IntegrityError('首次正文校验失败，保留损坏证据')
        actual = resource_fingerprint(metadata.get('parsed_sha256'), metadata.get('iframe_url_original'),
                                      extract_assets(content_html, base_url=metadata.get('iframe_url_original')))
        if actual != fingerprint or (metadata.get('resource_fingerprint') and metadata['resource_fingerprint'] != fingerprint):
            state.update_item(key, status='parsed', assets_done_at=None)
            raise IntegrityError('当前资源集合与解析正文不一致，请重新运行 parse')
        totals["items"] += 1
        totals["discovered"] += len(assets)

        assets_dir = item_dir / "assets"
        downloadable = []
        aliases = {}
        reused = {}
        for asset in assets:
            if validate_downloaded_asset(config, asset)['ok']:
                reused[resource_identity(asset)] = asset
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
            identity = resource_identity(asset)
            if identity in reused and not force:
                saved = reused[identity]
                state.record_asset_result(key, asset['original_url'], asset['attribute'],
                    **{name: saved[name] for name in ('download_attempted', 'download_status', 'http_status',
                        'content_type', 'local_path', 'sha256', 'size', 'download_identity')}, error=None)
                continue
            if identity in aliases:
                aliases[identity].append(asset)
                continue
            aliases[identity] = [asset]
            downloadable.append((position, asset))

        # complete 描述已有阶段进度；本次新增 media scope 仍需检查资源集合。
        metadata = read_json(item_dir / 'metadata.json', {}) or {}
        fingerprint = resource_fingerprint(metadata.get('parsed_sha256'), metadata.get('iframe_url_original'), assets)
        try:
            local_valid = current_content_path(item_dir) == item_dir / 'content.local.html'
        except IntegrityError:
            local_valid = False
        if (item.get('status') == 'complete' and article_truth(config, state, key)['complete']
                and local_valid and not force and not downloadable and
                (item_dir / 'content.local.html').is_file() and
                metadata.get('localization_version') == LOCALIZATION_VERSION and
                metadata.get('localized_resource_fingerprint') == fingerprint):
            continue

        if downloadable:
            assets_dir.mkdir(parents=True, exist_ok=True)
            with closing(bounded_results(
                downloadable, lambda pair: _download_one(client, config, pair[1], assets_dir, pair[0]),
                workers=config.concurrency, control=control,
            )) as results:
                for (_, asset), future in results:
                    try:
                        outcome = future.result()
                    except AuthExpired as exc:
                        aborted = str(exc)
                        control.stop()
                        continue
                    except TaskStopped:
                        continue
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
                    for reference in aliases[resource_identity(asset)]:
                        state.record_asset_result(key, reference['original_url'], reference['attribute'], **outcome)
        if aborted:
            break
        control.check()

        # ---- 重写本地版 + manifest ----
        refreshed = state.assets_for(key)
        mapping = {
            a["original_url"]: a["local_path"]
            for a in refreshed
            if validate_downloaded_asset(config, a)['ok']
        }
        # resolved_url 与 original_url 不同时（相对路径），两个键都要能命中
        for a in refreshed:
            if validate_downloaded_asset(config, a)['ok'] and a.get("resolved_url"):
                mapping.setdefault(a["resolved_url"], a["local_path"])

        localized_sha = write_text_atomic(item_dir / "content.local.html", localize_html(content_html, mapping))

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

        downloadable_refs = {(a['original_url'], a['attribute']) for a in refreshed if a.get('downloadable')}
        expected = [m for m in manifest if m['download_attempted'] or
                    ((m['original_url'], m['attribute']) in downloadable_refs and (
                    (m['kind'] == 'image' and config.download_images and m['resolved_url']) or
                    (want_media and m['kind'] not in {'image', 'link'} and m['resolved_url'])))]
        failed_here = [m for m in expected if m["download_status"] != "ok"]
        final_status = "assets_partial" if failed_here else ("warning" if item.get("warning") else "complete")
        index = index_truth(state)
        if final_status == 'complete' and (not metadata.get('parsed_sha256') or not index['ok'] or key not in index['seen_keys']):
            final_status = 'parsed'

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
                metadata["localized_from_sha256"] = metadata.get("parsed_sha256")
                metadata["localized_sha256"] = localized_sha
                metadata['localization_version'] = LOCALIZATION_VERSION
                metadata['resource_fingerprint'] = fingerprint
                metadata['localized_resource_fingerprint'] = fingerprint
                metadata['include_media'] = want_media or metadata.get('include_media', False)
                metadata["status"] = final_status
                write_json_atomic(metadata_path, metadata)

        if failed_here:
            state.update_item(key, status="assets_partial", assets_done_at=utcnow_iso())
            log("ASSET", f"key={key} {len(expected)-len(failed_here)}/{len(expected)} 下载成功（部分失败）")
        else:
            if item.get("warning"):
                state.update_item(key, status="warning", assets_done_at=utcnow_iso())
            else:
                state.update_item(key, status=final_status, assets_done_at=utcnow_iso())
            if expected:
                log("ASSET", f"key={key} {len(expected)}/{len(expected)} 全部下载成功")

    if aborted:
        log("AUTH", aborted)
    totals['incomplete_items'] = sum(not article_truth(config, state, r['key'])['complete'] for r in records)
    totals["aborted"] = aborted
    log("ASSET", f"完成：发现 {totals['discovered']}，尝试 {totals['attempted']}，"
                 f"成功 {totals['downloaded']}，失败 {totals['failed']}，仍未完整 {totals['incomplete_items']} 篇")
    return totals
