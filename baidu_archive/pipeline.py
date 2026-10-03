"""四个可独立重跑的阶段：index / fetch / parse / assets。

FETCH 与 PARSE 完全解耦 —— parse 只读 ``raw/``，不发任何网络请求。
解析器修 bug 后可 ``parse --force`` 重建全部派生数据，无需再访问百度。
"""

from __future__ import annotations

import csv
from contextlib import closing
from typing import Any, Callable

from .client import ArchiveClient, AuthExpired, FetchError
from .config import Config
from .parsers import (
    extract_assets,
    parse_content_html,
    parse_detail_html,
    parse_list_html,
    parse_list_json,
)
from .state import ArchiveState
from .tasks import TaskControl, TaskStopped, bounded_results
from .storage import IntegrityError, current_content_path, read_json, read_raw, save_raw, write_json_atomic, write_once, write_text_atomic
from .utils import redact_url, safe_key_dirname, sha256_bytes, sha256_file, truncate, utcnow_iso

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
    """Record current observations separately from the preserved historical index."""
    from uuid import uuid4
    config.ensure_dirs()
    control = getattr(client, 'control', None) or TaskControl()
    run_id = uuid4().hex
    generation = {'run_id': run_id, 'status': 'running', 'complete': False,
                  'seen_keys': [], 'pages': [], 'parsed_count': 0, 'declared_total': None,
                  'declared_totals': [], 'errors': [], 'pagination_complete': False}

    def persist():
        state.set_meta('index_generation', generation)
        state.set_meta('index_run:' + run_id, generation)

    persist()  # An interrupted new attempt cannot inherit an old complete result.
    start, pages_done = 0, 0
    seen, duplicates = set(), []
    missing = 0
    limit = config.page_limit
    try:
        while True:
            control.check()
            url = config.list_url(start, limit)
            raw_path = config.lists_dir / f'list_start{start:05d}.json'
            if raw_path.exists() and not force:
                body = read_raw(raw_path).content
            else:
                response = client.get(url, expect='json')
                body = response.content
                save_raw(raw_path, body, url=url, status_code=response.status_code,
                         headers=response.headers, extra={'stage': 'list', 'start': start,
                                                          'limit': limit, 'index_run_id': run_id})
            parsed = parse_list_json(body)
            declared = parsed['total_count']
            generation['declared_totals'].append(declared)
            if generation['declared_total'] is None:
                generation['declared_total'] = declared
                state.set_meta('total_count', declared)
            elif declared != generation['declared_total']:
                generation['errors'].append('total_count_changed')
            page_limit = parsed['limit']
            records = parse_list_html(parsed['html'], offset=start, site_root=config.site_root)
            page_errors = []
            if parsed['start'] != start:
                page_errors.append('unexpected_start')
            if not isinstance(page_limit, int) or page_limit <= 0 or page_limit != limit:
                page_errors.append('unexpected_limit')
            if (isinstance(declared, int) and isinstance(page_limit, int) and
                    parsed['current_count'] != min(page_limit, max(0, declared - start))):
                page_errors.append('unexpected_page_count')
            if parsed['current_count'] != len(records):
                page_errors.append('parsed_count_mismatch')
            for record in records:
                if not record.get('key'):
                    missing += 1
                    page_errors.append('missing_key')
                    continue
                key = record['key']
                if key in seen:
                    duplicates.append(key)
                    page_errors.append('duplicate_key')
                seen.add(key)
                state.upsert_record(record)
                state.ensure_item(key, 'pending' if record['type'] == 'page' else 'skipped_non_page')
                if record['type'] != 'page':
                    state.update_item(key, status='skipped_non_page')
            pages_done += 1
            generation['seen_keys'] = sorted(seen)
            generation['parsed_count'] += len(records)
            generation['pages'].append({'start': start, 'limit': page_limit,
                'declared_total': declared, 'declared_count': parsed['current_count'],
                'parsed_count': len(records), 'errors': page_errors})
            generation['errors'].extend(page_errors)
            persist()
            log('INDEX', f'page {pages_done} — observed {len(seen)} / declared {declared}')
            step = page_limit if isinstance(page_limit, int) and page_limit > 0 else limit
            start += step
            if isinstance(declared, int) and start >= declared:
                generation['pagination_complete'] = True
                break
            if max_pages and pages_done >= max_pages:
                generation['errors'].append('page_limit_reached')
                break
            if parsed['current_count'] == 0:
                generation['errors'].append('premature_empty_page')
                break
        control.check()
        expected = generation['declared_total']
        if not isinstance(expected, int) or len(seen) != expected:
            generation['errors'].append('observed_total_mismatch')
        generation['complete'] = generation['pagination_complete'] and not generation['errors']
        generation['status'] = 'completed' if generation['complete'] else 'partial'
        persist()
        if generation['complete']:
            state.set_meta('index_last_complete', generation)
    except BaseException as exc:
        generation['status'] = 'stopped' if isinstance(exc, (TaskStopped, KeyboardInterrupt)) else 'failed'
        generation['errors'].append(type(exc).__name__)
        persist()
        raise
    stats = {'total_count_api': generation['declared_total'], 'pages_fetched': pages_done,
             'unique_keys': len(seen), 'duplicate_keys': sorted(set(duplicates)),
             'rows_missing_key': missing, 'indexed': len(seen), 'historical_indexed': state.count_records(),
             'types': state.type_counts(), 'index_run_id': run_id,
             'index_complete': generation['complete'], 'failed': int(not generation['complete'])}
    state.set_meta('index_stats', stats)
    state.set_meta('indexed_at', utcnow_iso())
    export_master_index(config, state)
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
                "list_source": row["list_source"],
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
        "global_position", "key", "type", "title", "list_date", "tags", "list_source",
        "list_offset", "position_in_page", "detail_url", "external_url", "list_desc",
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
    detail_path = config.detail_dir / f"{safe_key_dirname(key)}.html"

    # ---- 1. 详情页 ----
    try:
        if detail_path.exists() and not force:
            snapshot = read_raw(detail_path)
            detail_html = snapshot.text
            detail_sha = snapshot.sha256
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
    content_path = config.content_dir / f"{safe_key_dirname(key)}.html"
    try:
        if content_path.exists() and not force:
            content_sha = read_raw(content_path).sha256
        else:
            try:
                response = client.get(iframe_url, expect="html")
            except FetchError as exc:
                # 签名 URL 可能已过期 —— 回详情页重取当前 iframe src，绝不自行计算 sign
                if exc.status in (401, 403, 404) or exc.status is None:
                    log("RETRY", f"key={key} 正文签名可能过期，回详情页重取 iframe")
                    fresh = client.get(detail_url, expect="html")
                    refreshed_meta = save_raw(detail_path, fresh.content, url=detail_url,
                                              status_code=fresh.status_code, headers=fresh.headers,
                                              extra={"stage": "detail", "key": key, "refreshed": True})
                    state.update_item(key, detail_sha256=refreshed_meta["sha256"], detail_fetched_at=utcnow_iso())
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
        if item.get("content_sha256") != content_sha:
            # 新的原始正文需要重新派生；旧版文件保留，当前版本的状态重新开始。
            state.update_item(key, status="content_fetched", parsed_at=None, assets_done_at=None)
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
    if only_keys is not None:
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

    control = getattr(client, "control", None) or TaskControl()
    with closing(bounded_results(
        records, lambda record: _fetch_one(config, state, client, record, log, force),
        workers=config.concurrency, control=control,
    )) as results:
        try:
            for record, future in results:
                done += 1
                try:
                    result = future.result()
                except AuthExpired as exc:
                    aborted = str(exc)
                    control.stop()
                    state.update_item(record["key"], status="auth_error", last_error="认证失效")
                    continue
                except TaskStopped:
                    continue
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
    if control.stopped and not aborted:
        raise TaskStopped("抓取已停止，已保存数据可续传")

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
    only_keys: list[str] | None = None,
    control: TaskControl | None = None,
) -> dict[str, Any]:
    """从 raw/ 重建 metadata.json + content.original.html。不发任何网络请求。"""
    config.ensure_dirs()
    records = state.iter_records("page")
    if only_keys is not None:
        wanted = set(only_keys)
        records = [record for record in records if record["key"] in wanted]
    if limit:
        records = records[:limit]

    control = control or TaskControl()
    from .truth import resource_fingerprint
    counters = {"parsed": 0, "warning": 0, "missing_raw": 0, "skip": 0}
    for record in records:
        control.check()
        key = record["key"]
        item = state.get_item(key) or {}
        content_base = config.content_dir / f"{safe_key_dirname(key)}.html"
        if not content_base.exists():
            counters["missing_raw"] += 1
            continue

        content_snapshot = read_raw(content_base)
        content_path = content_snapshot.path
        detail_base = config.detail_dir / f"{safe_key_dirname(key)}.html"
        detail_snapshot = read_raw(detail_base) if detail_base.exists() else None
        detail_path = detail_snapshot.path if detail_snapshot else detail_base

        item_dir = config.item_dir(key)
        item_dir.mkdir(parents=True, exist_ok=True)
        previous = read_json(item_dir / "metadata.json", {}) or {}
        try:
            parsed_current = current_content_path(item_dir, prefer_local=False).is_file()
        except (IntegrityError, OSError):
            parsed_current = False
        if (parsed_current and previous.get('resource_fingerprint') == resource_fingerprint(
                previous.get('parsed_sha256'), previous.get('iframe_url_original'), state.assets_for(key))
                and item.get("status") in {"parsed", "assets_partial", "complete"} and not force
                and previous.get("content_sha256") == content_snapshot.sha256
                and previous.get("detail_sha256") == (detail_snapshot.sha256 if detail_snapshot else None)):
            counters["skip"] += 1
            continue

        parsed = parse_content_html(content_snapshot.text)
        detail_info = parse_detail_html(detail_snapshot.text if detail_snapshot else "", base_url=record.get("detail_url"))

        content_html = parsed["content_html"] or ""
        original = item_dir / "content.original.html"
        if not original.exists() and parsed["has_container"]:
            write_once(original, content_html.encode("utf-8"))
        original_sha = sha256_file(original) if original.exists() else None
        if previous.get("original_sha256") and previous["original_sha256"] != original_sha:
            raise IntegrityError(f"首次正文校验失败：{key}")
        parsed_sha = sha256_bytes(content_html.encode("utf-8")) if parsed["has_container"] else None
        if parsed_sha:
            write_once(item_dir / "parsed" / f"{parsed_sha}.html", content_html.encode("utf-8"))

        import json as _json

        warnings: list[str] = []
        if not parsed["has_container"]:
            warnings.append("源站返回页面不存在，正文尚未取回" if parsed["page_error"] == "source_unavailable"
                            else "未找到正文容器（页面结构可能已变更）")
        if parsed["content_length"] == 0 and not parsed["has_media"]:
            warnings.append("正文长度为 0")
        if not parsed["has_title"]:
            warnings.append("未找到标题节点")
        if not parsed["has_footer"]:
            warnings.append("未找到 footer（日期/来源可能缺失）")
        list_desc = record.get("list_desc") or ""
        if len(list_desc) > 50 and parsed["content_length"] == 0 and not parsed["has_media"]:
            warnings.append("列表摘要有内容但正文为空 —— 高度可疑")

        base_url = detail_info.get("iframe_url") or item.get("iframe_url")
        assets = extract_assets(content_html, base_url=base_url)
        fingerprint = resource_fingerprint(parsed_sha, base_url, assets)
        same_parsed = previous.get('resource_fingerprint') == fingerprint
        state.update_item(key, iframe_url=base_url)
        for asset in assets:
            state.upsert_asset(key, asset)
        state.set_current_assets(key, assets)

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
            # 列表标签与正文页标签各存一份；正文页还带用户备注 note
            "tags": _json.loads(record.get("tags_json") or "[]"),
            "content_tags": parsed.get("content_tags") or [],
            "note": parsed.get("note"),
            # 正文页声明 <base href="http://1">，是占位符，仅记录不采信
            "content_base_href": parsed.get("base_href"),
            # 列表来源与正文 footer 来源各存一份，不擅自判断谁"正确"
            "source": parsed["source"] or record.get("list_source"),
            "content_source": parsed["source"],
            "list_source": record.get("list_source"),
            "list_desc": record.get("list_desc"),
            "detail_url": record.get("detail_url"),
            "external_url": record.get("external_url"),
            # 完整签名 URL 只存在于本地 metadata，用于将来复原；日志/报告中一律打码
            "iframe_url_original": base_url,
            "iframe_url_redacted": redact_url(base_url or ""),
            "list_offset": record.get("list_offset"),
            "position_in_page": record.get("position_in_page"),
            "global_position": record.get("global_position"),
            "content_length": parsed["content_length"],
            "content_html_length": parsed["content_html_length"],
            "has_content_container": parsed["has_container"],
            "has_media": parsed["has_media"],
            "page_error": parsed["page_error"],
            "raw_detail_path": str(detail_path.relative_to(config.root)) if detail_path.exists() else None,
            "raw_content_path": str(content_path.relative_to(config.root)),
            "detail_sha256": detail_snapshot.sha256 if detail_snapshot else None,
            "content_sha256": content_snapshot.sha256,
            "original_sha256": original_sha,
            "original_raw_content_path": (previous.get("original_raw_content_path") or
                (previous.get("raw_content_path") if previous.get("original_sha256") else None) or
                (str(content_path.relative_to(config.root)) if original_sha else None)),
            "original_raw_content_sha256": (previous.get("original_raw_content_sha256") or
                (previous.get("content_sha256") if previous.get("original_sha256") else None) or
                (content_snapshot.sha256 if original_sha else None)),
            "parsed_sha256": parsed_sha,
            "resource_fingerprint": fingerprint,
            "localized_resource_fingerprint": previous.get('localized_resource_fingerprint') if same_parsed else None,
            "include_media": previous.get('include_media', False),
            "parsed_content_path": f"parsed/{parsed_sha}.html" if parsed_sha else None,
            "localized_from_sha256": previous.get("localized_from_sha256") if same_parsed else None,
            "localized_sha256": previous.get("localized_sha256") if same_parsed else None,
            "localization_version": previous.get("localization_version") if same_parsed else None,
            "assets": previous.get("assets", []) if same_parsed else [],
            "asset_count_discovered": len(assets),
            "archived_at": utcnow_iso(),
            "first_attempt_at": item.get("first_attempt_at"),
            "last_attempt_at": item.get("last_attempt_at"),
            "status": "warning" if warnings else (
                item.get("status") if same_parsed and item.get("status") in {"complete", "assets_partial"} else "parsed"
            ),
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
            detail_sha256=detail_snapshot.sha256 if detail_snapshot else None,
            content_sha256=content_snapshot.sha256,
        )
        if not same_parsed:
            state.update_item(key, status="parsed", assets_done_at=None)
        if warnings:
            state.update_item(key, status="warning")
            counters["warning"] += 1
            log("WARN", f"key={key} — {'; '.join(warnings)}")
        else:
            counters["parsed"] += 1

    control.check()
    log("PARSE", f"完成：解析 {counters['parsed']}，警告 {counters['warning']}，"
                 f"缺少 raw {counters['missing_raw']}，跳过 {counters['skip']}")
    return counters
