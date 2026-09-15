"""四个可独立重跑的阶段：index / fetch / parse / assets。

FETCH 与 PARSE 完全解耦 —— parse 只读 ``raw/``，不发任何网络请求。
解析器修 bug 后可 ``parse --force`` 重建全部派生数据，无需再访问百度。
"""

from __future__ import annotations

import csv
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .client import ArchiveClient, AuthExpired, FetchError
from .config import Config
from .parsers import (
    ListResponseError,
    extract_assets,
    parse_content_html,
    parse_detail_html,
    parse_list_html,
    parse_list_json,
)
from .state import ArchiveState
from .storage import read_text, save_raw, write_json_atomic, write_text_atomic
from .utils import redact_url, sha256_bytes, truncate, utcnow_iso

Logger = Callable[[str, str], None]


def _noop(tag: str, message: str) -> None:  # pragma: no cover
    print(f"[{tag}] {message}")


# ==========================================================================
# INDEX
# ==========================================================================

def run_index(
    config: Config,
    state: ArchiveState,
    client: ArchiveClient,
    *,
    log: Logger = _noop,
    force: bool = False,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """翻完所有分页，落 raw JSON，建立全部 504 条 master index。"""
    config.ensure_dirs()
    limit = config.page_limit
    start = 0
    total_count: int | None = None
    pages_done = 0
    seen_keys: set[str] = set()
    duplicates: list[str] = []
    missing_key_rows = 0

    while True:
        url = config.list_url(start, limit)
        raw_path = config.lists_dir / f"list_start{start:05d}.json"

        if raw_path.exists() and not force:
            body = raw_path.read_bytes()
            log("INDEX", f"start={start} 复用已有 raw（--force 可强制重取）")
        else:
            response = client.get(url, expect="json")
            body = response.content
            save_raw(
                raw_path,
                body,
                url=url,
                status_code=response.status_code,
                headers=response.headers,
                extra={"stage": "list", "start": start, "limit": limit},
            )

        try:
            parsed = parse_list_json(body)
        except ListResponseError as exc:
            state.record_failure(None, "index", url_redacted=redact_url(url), error=str(exc))
            raise

        if total_count is None:
            total_count = parsed["total_count"]
            state.set_meta("total_count", total_count)
            state.set_meta("page_limit", parsed["limit"] or limit)
            expected_pages = math.ceil(total_count / (parsed["limit"] or limit)) if total_count else 0
            log("INDEX", f"API totalCount = {total_count}，预计 {expected_pages} 页")

        records = parse_list_html(parsed["html"], offset=parsed["start"] or start, site_root=config.site_root)
        pages_done += 1
        page_no = pages_done
        total_pages = math.ceil((total_count or 0) / limit) if total_count else "?"
        log("INDEX", f"page {page_no}/{total_pages} — 解析出 {len(records)} 条")

        for record in records:
            if not record.get("key"):
                missing_key_rows += 1
                state.record_failure(None, "index", error="列表项缺少 key", url_redacted=redact_url(url))
                continue
            if record["key"] in seen_keys:
                duplicates.append(record["key"])
            seen_keys.add(record["key"])
            state.upsert_record(record)
            if record["type"] == "page":
                state.ensure_item(record["key"], "pending")
            else:
                state.ensure_item(record["key"], "skipped_non_page")
                state.update_item(record["key"], status="skipped_non_page")

        current = parsed["current_count"] or len(records)
        start += parsed["limit"] or limit
        if max_pages and pages_done >= max_pages:
            log("INDEX", f"已达 --max-pages {max_pages}，提前结束")
            break
        if total_count and start >= total_count:
            break
        if current == 0:
            log("INDEX", "本页返回 0 条，提前结束")
            break

    stats = {
        "total_count_api": total_count,
        "pages_fetched": pages_done,
        "unique_keys": len(seen_keys),
        "duplicate_keys": sorted(set(duplicates)),
        "rows_missing_key": missing_key_rows,
        "indexed": state.count_records(),
        "types": state.type_counts(),
    }
    state.set_meta("index_stats", stats)
    state.set_meta("indexed_at", utcnow_iso())
    export_master_index(config, state)
    log("INDEX", f"完成：索引 {stats['indexed']} 条 / API 声明 {total_count}，类型 {stats['types']}")
    return stats


def export_master_index(config: Config, state: ArchiveState) -> None:
    """导出 master_index.json / .csv / crawl_state.json（派生产物，保留原始顺序）。"""
    import json

    records = state.iter_records()
    payload = []
    for row in records:
        payload.append(
            {
                "key": row["key"],
                "type": row["type"],
                "title": row["title"],
                "list_date": row["list_date"],
                "list_date_text": row["list_date_text"],
                "tags": json.loads(row["tags_json"] or "[]"),
                "list_desc": row["list_desc"],
                "detail_url": row["detail_url"],
                "external_url": row["external_url"],
                "list_offset": row["list_offset"],
                "position_in_page": row["position_in_page"],
                "global_position": row["global_position"],
                "first_seen_at": row["first_seen_at"],
            }
        )
    write_json_atomic(config.root / "master_index.json", payload)

    csv_path = config.root / "master_index.csv"
    columns = [
        "global_position", "key", "type", "title", "list_date",
        "tags", "list_offset", "position_in_page", "detail_url", "external_url", "list_desc",
    ]
    lines = []
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for entry in payload:
        row = dict(entry)
        row["tags"] = "|".join(row.get("tags") or [])
        # CSV 里正文摘要截断，避免单元格超限；完整内容在 JSON 与 items/ 中
        row["list_desc"] = truncate(row.get("list_desc") or "", 200)
        writer.writerow(row)
    lines.append(buffer.getvalue())
    write_text_atomic(csv_path, "﻿" + "".join(lines))

    write_json_atomic(
        config.root / "crawl_state.json",
        {
            "generated_at": utcnow_iso(),
            "total_count_api": state.get_meta("total_count"),
            "indexed": len(payload),
            "types": state.type_counts(),
            "statuses": state.status_counts(),
        },
    )


# ==========================================================================
# FETCH
# ==========================================================================

def _fetch_one(
    config: Config,
    state: ArchiveState,
    client: ArchiveClient,
    record: dict[str, Any],
    log: Logger,
    force: bool,
) -> str:
    """抓单条：detail → 解析 iframe → content。返回结果标签。"""
    key = record["key"]
    item = state.get_item(key) or {}
    status = item.get("status", "pending")

    if status == "complete" and not force:
        return "skip"
    if status == "content_fetched" and not force:
        # detail 与 content 都已落盘，交给 parse 阶段
        return "skip"

    state.mark_attempt(key)
    detail_url = record.get("detail_url") or config.detail_url(key, record.get("type") or "page")
    detail_path = config.detail_dir / f"{key}.html"

    # ---- 1. 详情页 ----
    try:
        if detail_path.exists() and not force:
            detail_html = read_text(detail_path) or ""
            detail_sha = sha256_bytes(detail_html.encode("utf-8"))
        else:
            response = client.get(detail_url, expect="html")
            meta = save_raw(
                detail_path,
                response.content,
                url=detail_url,
                status_code=response.status_code,
                headers=response.headers,
                extra={"stage": "detail", "key": key},
            )
            detail_html = response.text
            detail_sha = meta["sha256"]
        state.advance_status(key, "detail_fetched", detail_fetched_at=utcnow_iso(), detail_sha256=detail_sha)
    except FetchError as exc:
        state.update_item(key, status="failed", last_error=f"detail: {exc}")
        state.record_failure(key, "detail", url_redacted=redact_url(detail_url),
                             http_status=exc.status, error=str(exc), attempt=exc.attempts)
        log("FAIL", f"key={key} detail — {exc}")
        return "failed"

    # ---- 2. 解析 iframe ----
    detail_info = parse_detail_html(detail_html, base_url=detail_url)
    iframe_url = detail_info.get("iframe_url")
    if not iframe_url:
        state.update_item(key, status="failed", last_error="详情页未找到 iframe")
        state.record_failure(key, "iframe", url_redacted=redact_url(detail_url), error="未找到 iframe src")
        log("FAIL", f"key={key} — 详情页未找到 iframe")
        return "failed"
    state.update_item(key, iframe_url=iframe_url)

    # ---- 3. 正文页 ----
    content_path = config.content_dir / f"{key}.html"
    try:
        if content_path.exists() and not force:
            content_sha = sha256_bytes((read_text(content_path) or "").encode("utf-8"))
        else:
            try:
                response = client.get(iframe_url, expect="html")
            except FetchError as exc:
                # 签名 URL 可能已过期 —— 回详情页重取当前 iframe src，绝不自行计算 sign
                if exc.status in (401, 403, 404) or exc.status is None:
                    log("RETRY", f"key={key} 正文签名可能过期，回详情页重取 iframe")
                    fresh = client.get(detail_url, expect="html")
                    save_raw(detail_path, fresh.content, url=detail_url,
                             status_code=fresh.status_code, headers=fresh.headers,
                             extra={"stage": "detail", "key": key, "refreshed": True})
                    refreshed = parse_detail_html(fresh.text, base_url=detail_url).get("iframe_url")
                    if not refreshed:
                        raise
                    iframe_url = refreshed
                    state.update_item(key, iframe_url=iframe_url)
                    response = client.get(iframe_url, expect="html")
                else:
                    raise
            meta = save_raw(
                content_path,
                response.content,
                url=iframe_url,
                status_code=response.status_code,
                headers=response.headers,
                extra={"stage": "content", "key": key},
            )
            content_sha = meta["sha256"]
        state.advance_status(key, "content_fetched", content_fetched_at=utcnow_iso(), content_sha256=content_sha)
        state.clear_failures(key)
        return "ok"
    except FetchError as exc:
        state.update_item(key, status="failed", last_error=f"content: {exc}")
        state.record_failure(key, "content", url_redacted=redact_url(iframe_url),
                             http_status=exc.status, error=str(exc), attempt=exc.attempts)
        log("FAIL", f"key={key} content — {exc}")
        return "failed"


def run_fetch(
    config: Config,
    state: ArchiveState,
    client: ArchiveClient,
    *,
    log: Logger = _noop,
    force: bool = False,
    limit: int | None = None,
    only_keys: list[str] | None = None,
) -> dict[str, Any]:
    """抓取所有 page 类型的 detail + content 原始 HTML。"""
    config.ensure_dirs()
    records = [r for r in state.iter_records("page")]
    if only_keys:
        wanted = set(only_keys)
        records = [r for r in records if r["key"] in wanted]
    if not force:
        records = [
            r for r in records
            if (state.get_item(r["key"]) or {}).get("status") not in {"complete", "content_fetched", "parsed", "assets_partial"}
        ]
    if limit:
        records = records[:limit]

    total = len(records)
    if total == 0:
        log("FETCH", "没有待抓取的条目（全部已完成）")
        return {"total": 0, "ok": 0, "failed": 0, "skipped": 0}

    log("FETCH", f"待抓取 {total} 条，并发 {config.concurrency}，间隔 {config.delay}s")
    counters = {"ok": 0, "failed": 0, "skip": 0}
    done = 0
    aborted: str | None = None

    with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as pool:
        futures = {
            pool.submit(_fetch_one, config, state, client, record, log, force): record
            for record in records
        }
        try:
            for future in as_completed(futures):
                record = futures[future]
                done += 1
                try:
                    result = future.result()
                except AuthExpired as exc:
                    aborted = str(exc)
                    for pending in futures:
                        pending.cancel()
                    break
                except Exception as exc:  # noqa: BLE001 —— 单条异常不应终止整批
                    counters["failed"] += 1
                    state.update_item(record["key"], status="failed", last_error=str(exc)[:500])
                    state.record_failure(record["key"], "fetch", error=f"{type(exc).__name__}: {exc}"[:500])
                    log("FAIL", f"key={record['key']} — {type(exc).__name__}: {exc}")
                    continue
                counters[result] = counters.get(result, 0) + 1
                if result == "ok":
                    log("FETCH", f"{done}/{total} key={record['key']} {truncate(record.get('title') or '', 30)}")
                elif result == "skip":
                    log("SKIP", f"{done}/{total} key={record['key']} 已完成")
        except AuthExpired as exc:
            aborted = str(exc)

    stats = {"total": total, **counters, "aborted": aborted}
    if aborted:
        log("AUTH", aborted)
    log("FETCH", f"完成：成功 {counters['ok']}，失败 {counters['failed']}，跳过 {counters['skip']}")
    return stats


# ==========================================================================
# PARSE（纯离线：只读 raw/）
# ==========================================================================

def run_parse(
    config: Config,
    state: ArchiveState,
    *,
    log: Logger = _noop,
    force: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """从 raw/ 重建 metadata.json + content.original.html。不发任何网络请求。"""
    config.ensure_dirs()
    records = state.iter_records("page")
    if limit:
        records = records[:limit]

    counters = {"parsed": 0, "warning": 0, "missing_raw": 0, "skip": 0}
    for record in records:
        key = record["key"]
        item = state.get_item(key) or {}
        if item.get("status") in {"parsed", "assets_partial", "complete"} and not force:
            counters["skip"] += 1
            continue

        content_path = config.content_dir / f"{key}.html"
        if not content_path.exists():
            counters["missing_raw"] += 1
            continue

        raw_html = read_text(content_path) or ""
        parsed = parse_content_html(raw_html)
        detail_path = config.detail_dir / f"{key}.html"
        detail_info = parse_detail_html(read_text(detail_path) or "", base_url=record.get("detail_url"))

        item_dir = config.item_dir(key)
        item_dir.mkdir(parents=True, exist_ok=True)

        content_html = parsed["content_html"] or ""
        # content.original.html 是正文容器的 inner HTML 原样，之后永不被任何阶段修改
        write_text_atomic(item_dir / "content.original.html", content_html)

        import json as _json

        warnings: list[str] = []
        if not parsed["has_container"]:
            warnings.append("未找到正文容器（页面结构可能已变更）")
        if parsed["content_length"] == 0:
            warnings.append("正文长度为 0")
        if not parsed["has_title"]:
            warnings.append("未找到标题节点")
        if not parsed["has_footer"]:
            warnings.append("未找到 footer（日期/来源可能缺失）")
        list_desc = record.get("list_desc") or ""
        if len(list_desc) > 50 and parsed["content_length"] == 0:
            warnings.append("列表摘要有内容但正文为空 —— 高度可疑")

        assets = extract_assets(content_html, base_url=item.get("iframe_url") or detail_info.get("iframe_url"))
        for asset in assets:
            state.upsert_asset(key, asset)

        metadata = {
            "key": key,
            "type": record["type"],
            "title": parsed["title"] or record.get("title"),
            "list_title": record.get("title"),
            "content_title": parsed["title"],
            "list_date": record.get("list_date"),
            "list_date_text": record.get("list_date_text"),
            "content_date": parsed["content_date"],
            "content_date_text": parsed["content_date_text"],
            "tags": _json.loads(record.get("tags_json") or "[]"),
            "source": parsed["source"],
            "list_desc": record.get("list_desc"),
            "detail_url": record.get("detail_url"),
            "external_url": record.get("external_url"),
            # 完整签名 URL 只存在于本地 metadata，用于将来复原；日志/报告中一律打码
            "iframe_url_original": item.get("iframe_url") or detail_info.get("iframe_url"),
            "iframe_url_redacted": redact_url(item.get("iframe_url") or detail_info.get("iframe_url") or ""),
            "list_offset": record.get("list_offset"),
            "position_in_page": record.get("position_in_page"),
            "global_position": record.get("global_position"),
            "content_length": parsed["content_length"],
            "content_html_length": parsed["content_html_length"],
            "raw_detail_path": str(detail_path.relative_to(config.root)) if detail_path.exists() else None,
            "raw_content_path": str(content_path.relative_to(config.root)),
            "detail_sha256": item.get("detail_sha256"),
            "content_sha256": item.get("content_sha256"),
            "assets": [],
            "asset_count_discovered": len(assets),
            "archived_at": utcnow_iso(),
            "first_attempt_at": item.get("first_attempt_at"),
            "last_attempt_at": item.get("last_attempt_at"),
            "status": "warning" if warnings else "parsed",
            "warnings": warnings,
            "errors": [],
        }
        write_json_atomic(item_dir / "metadata.json", metadata)

        state.advance_status(
            key,
            "parsed",
            parsed_at=utcnow_iso(),
            content_title=parsed["title"],
            content_date=parsed["content_date"],
            content_date_text=parsed["content_date_text"],
            source=parsed["source"],
            content_length=parsed["content_length"],
            warning="; ".join(warnings) or None,
        )
        if warnings:
            state.update_item(key, status="warning")
            counters["warning"] += 1
            log("WARN", f"key={key} — {'; '.join(warnings)}")
        else:
            counters["parsed"] += 1

    log("PARSE", f"完成：解析 {counters['parsed']}，警告 {counters['warning']}，"
                 f"缺少 raw {counters['missing_raw']}，跳过 {counters['skip']}")
    return counters
