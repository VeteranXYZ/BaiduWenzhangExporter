"""完整性校验。目标只有一个：让你一眼知道"到底有没有漏东西"。"""

from __future__ import annotations

import csv
import io
from typing import Any, Callable

from .config import Config
from .integrity import audit_stored_files
from .state import ArchiveState
from .truth import article_truth, index_truth, validate_downloaded_asset
from .storage import read_json, write_json_atomic, write_text_atomic
from .utils import redact_url, safe_key_dirname, truncate, utcnow_iso

Logger = Callable[[str, str], None]


def _noop(tag: str, message: str) -> None:  # pragma: no cover
    print(f"[{tag}] {message}")


def _csv_text(rows: list[dict[str, Any]], columns: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    # BOM 让 Excel 正确识别 UTF-8 中文
    return "﻿" + buffer.getvalue()


def validation_result(summary: dict[str, Any]) -> dict[str, Any]:
    """CLI 与操作台共用的完整性判定；只在所有要求都满足时通过。"""
    pages = summary['baidu_space_pages']
    expected, indexed = summary['api_total_count'], summary['indexed']
    checks = {
        'index_complete': summary['index_complete'] is True,
        'counts_match': isinstance(expected, int) and expected == indexed,
        'order_contiguous': summary['original_order']['contiguous'],
        'no_problem_items': summary['problem_items'] == 0,
        'pages_complete': pages['complete'] == pages['total'],
        'raw_content_present': pages['raw_content_on_disk'] == pages['total'],
        'original_content_present': pages['content_original_on_disk'] == pages['total'],
        'no_empty_content': pages['empty_content'] == 0,
        'no_failed_assets': summary['assets']['failed'] == 0,
        'files_intact': summary['stored_file_integrity']['ok'],
    }
    return {'ok': all(checks.values()), 'expected': expected, 'indexed': indexed,
            'missing': max(0, expected - indexed) if isinstance(expected, int) else None,
            'excess': max(0, indexed - expected) if isinstance(expected, int) else None,
            'problem_items': summary['problem_items'], 'checks': checks}


def run_verify(config: Config, state: ArchiveState, *, log: Logger = _noop) -> dict[str, Any]:
    config.ensure_dirs()

    index = index_truth(state)
    total_api = index['expected']
    indexed = index['indexed']
    types = state.type_counts()
    statuses = state.status_counts()

    records = state.iter_records()
    if index['run_id']:
        records = [r for r in records if r['key'] in set(index['seen_keys'])]
    keys = [r["key"] for r in records]
    unique_keys = set(keys)
    duplicate_keys = sorted({k for k in keys if keys.count(k) > 1}) if len(keys) != len(unique_keys) else []

    # ---- 原始顺序连续性：global_position 应为 0..N-1 无空洞 ----
    positions = sorted(r["global_position"] for r in records if r["global_position"] is not None)
    expected_positions = list(range(len(positions)))
    position_gaps = []
    if positions and positions != expected_positions:
        present = set(positions)
        position_gaps = [p for p in range(max(positions) + 1) if p not in present]

    # ---- page 类型逐阶段核对 ----
    page_records = [r for r in records if r["type"] == "page"]
    page_total = len(page_records)
    # 进度计数（由时间戳推导）与状态计数（由 status 字段推导）必须分开，
    # 否则 status=="parsed" 会同时命中两边而被重复累加。
    stage = {
        "detail_fetched": 0, "iframe_found": 0, "content_fetched": 0,
        "parsed": 0, "assets_complete": 0,
        "raw_detail_on_disk": 0, "raw_content_on_disk": 0,
        "content_original_on_disk": 0, "empty_content": 0,
        "source_unavailable": 0,
    }
    status_breakdown = {
        "complete": 0, "warning": 0, "failed": 0, "auth_error": 0,
        "pending": 0, "parsed": 0, "assets_partial": 0, "legacy_unverified": 0,
        "detail_fetched": 0, "content_fetched": 0,
    }
    problem_rows: list[dict[str, Any]] = []

    for record in page_records:
        key = record["key"]
        item = state.get_item(key) or {}
        status = article_truth(config, state, key)["status"]
        metadata = read_json(config.item_dir(key) / "metadata.json", {}) or {}
        if metadata.get("page_error") == "source_unavailable":
            stage["source_unavailable"] += 1

        if item.get("detail_fetched_at"):
            stage["detail_fetched"] += 1
        if item.get("iframe_url"):
            stage["iframe_found"] += 1
        if item.get("content_fetched_at"):
            stage["content_fetched"] += 1
        if item.get("parsed_at"):
            stage["parsed"] += 1
        status_breakdown[status] = status_breakdown.get(status, 0) + 1
        if status == "complete":
            stage["assets_complete"] += 1

        if (config.detail_dir / f"{safe_key_dirname(key)}.html").exists():
            stage["raw_detail_on_disk"] += 1
        if (config.content_dir / f"{safe_key_dirname(key)}.html").exists():
            stage["raw_content_on_disk"] += 1
        original = config.item_dir(key) / "content.original.html"
        if original.exists():
            stage["content_original_on_disk"] += 1
            try:
                if original.stat().st_size == 0:
                    stage["empty_content"] += 1
            except OSError:
                pass

        if status != "complete":
            problem_rows.append(
                {
                    "key": key,
                    "global_position": record.get("global_position"),
                    "title": truncate(record.get("title") or "", 60),
                    "status": status,
                    "attempts": item.get("attempts") or 0,
                    "warning": truncate(item.get("warning") or "", 120),
                    "last_error": truncate(item.get("last_error") or "", 160),
                    "detail_url": redact_url(record.get("detail_url") or ""),
                }
            )

    # ---- 资源 ----
    assets = [a for a in state.all_assets() if not index["run_id"] or a["key"] in set(index["seen_keys"])]
    asset_valid = {a["id"]: validate_downloaded_asset(config, a)["ok"] for a in assets}
    from .truth import resource_state
    legacy_count = sum(resource_state(config, a)['state'] == 'legacy_unverified' for a in assets)
    asset_stats = {
        'legacy_unverified': legacy_count,
        "discovered": len(assets),
        "downloadable": sum(1 for a in assets if a.get("downloadable")),
        "attempted": sum(1 for a in assets if a.get("download_attempted")),
        "downloaded": sum(1 for a in assets if asset_valid[a["id"]]),
        "failed": sum(1 for a in assets if a.get("download_attempted") and not asset_valid[a["id"]] and resource_state(config, a)["state"] != "legacy_unverified"),
        "non_http": sum(1 for a in assets if not a.get("downloadable")),
    }

    # ---- 重复（只报告，绝不自动删除）----
    duplicates = {
        "duplicate_keys": duplicate_keys,
        "duplicate_titles": [
            {"title": r["title"], "count": r["n"], "keys": r["keys"]}
            for r in state.duplicate_titles()
        ],
        "duplicate_content_hashes": [
            {"sha256": r["sha"], "count": r["n"], "keys": r["keys"]}
            for r in state.duplicate_content_hashes()
        ],
    }

    missing_count = (total_api - indexed) if isinstance(total_api, int) else None
    summary = {
        "generated_at": utcnow_iso(),
        "api_total_count": total_api,
        "indexed": indexed,
        "unique_records": len(unique_keys),
        "index_missing_vs_api": missing_count,
        "index_complete": index["ok"],
        "index_generation": index,
        "historical_indexed": state.count_records(),
        "types": types,
        "statuses": statuses,
        "original_order": {
            "positions_recorded": len(positions),
            "gaps": position_gaps[:50],
            "gap_count": len(position_gaps),
            "contiguous": positions == list(range(indexed)),
        },
        "baidu_space_pages": {
            "total": page_total,
            **stage,
            **status_breakdown,
            "stages": stage,
        },
        "assets": asset_stats,
        "duplicates": duplicates,
        "failures_logged": len(state.all_failures()),
        "problem_items": len(problem_rows),
        "stored_file_integrity": audit_stored_files(config, state),
    }

    summary['validation'] = validation_result(summary)
    write_json_atomic(config.reports_dir / "summary.json", summary)
    write_json_atomic(config.reports_dir / "stored_file_integrity.json", summary["stored_file_integrity"])
    write_text_atomic(
        config.reports_dir / "failed_items.csv",
        _csv_text(
            problem_rows,
            ["key", "global_position", "title", "status", "attempts", "warning", "last_error", "detail_url"],
        ),
    )

    # ---- 外链清单（含已失效的，失败本身也是档案信息）----
    external_rows = []
    for asset in assets:
        if asset["kind"] == "link" or (asset.get("domain") and "baidu" not in asset["domain"]):
            record = state.get_record(asset["key"]) or {}
            external_rows.append(
                {
                    "article_key": asset["key"],
                    "article_title": truncate(record.get("title") or "", 60),
                    "url": redact_url(asset.get("resolved_url") or asset.get("original_url") or ""),
                    "domain": asset.get("domain") or "",
                    "element_type": asset.get("element_type"),
                    "kind": asset.get("kind"),
                    "download_status": asset.get("download_status") or "not_attempted",
                    "http_status": asset.get("http_status") or "",
                }
            )
    write_text_atomic(
        config.reports_dir / "external_resources.csv",
        _csv_text(
            external_rows,
            ["article_key", "article_title", "url", "domain", "element_type", "kind",
             "download_status", "http_status"],
        ),
    )

    write_text_atomic(config.reports_dir / "summary.md", render_summary_md(summary))

    log("VERIFY", f"报告已生成 → {config.reports_dir}")
    return summary


def render_summary_md(summary: dict[str, Any]) -> str:
    pages = summary["baidu_space_pages"]
    stages = pages.get("stages", pages)
    assets = summary["assets"]
    order = summary["original_order"]
    duplicates = summary["duplicates"]

    def tick(ok: bool | None) -> str:
        return "✅" if ok else ("❌" if ok is False else "❓")

    lines = [
        "# 归档完整性报告",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        "## 索引完整性",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| API 声明 totalCount | {summary['api_total_count']} |",
        f"| 已建立索引 | {summary['indexed']} |",
        f"| 唯一记录 | {summary['unique_records']} |",
        f"| 与 API 差额 | {summary['index_missing_vs_api']} |",
        f"| **索引是否完整** | {tick(summary['index_complete'])} |",
        f"| 原始顺序连续 | {tick(order['contiguous'])}（空洞 {order['gap_count']} 处）|",
        "",
        "## 类型分布",
        "",
        "| 类型 | 数量 |",
        "|---|---|",
    ]
    for name, count in sorted(summary["types"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {name} | {count} |")

    lines += [
        "",
        "## 百度空间正文（page）",
        "",
        f"总计 {pages['total']} 条。",
        "",
        "流水线进度（按时间戳统计，累进）：",
        "",
        "| 阶段 | 数量 |",
        "|---|---|",
        f"| 详情页已抓 | {stages['detail_fetched']} |",
        f"| 找到 iframe | {stages['iframe_found']} |",
        f"| 正文页已抓 | {stages['content_fetched']} |",
        f"| 已解析 | {stages['parsed']} |",
        f"| 资源已完成 | {stages['assets_complete']} |",
        "",
        "当前状态分布（互斥，合计应等于总计）：",
        "",
        "| 状态 | 数量 |",
        "|---|---|",
        f"| complete | {pages['complete']} |",
        f"| warning | {pages['warning']} |",
        f"| assets_partial | {pages.get('assets_partial', 0)} |",
        f"| parsed | {pages['parsed_status'] if 'parsed_status' in pages else pages['parsed']} |",
        f"| failed | {pages['failed']} |",
        f"| auth_error | {pages['auth_error']} |",
        f"| pending | {pages['pending']} |",
        "",
        "落盘核对：",
        "",
        f"- raw 详情页：{pages['raw_detail_on_disk']} / {pages['total']}",
        f"- raw 正文页：{pages['raw_content_on_disk']} / {pages['total']}",
        f"- content.original.html：{pages['content_original_on_disk']} / {pages['total']}",
        f"- 正文为空：{pages['empty_content']}",
        f"- 源站返回页面不存在：{pages['source_unavailable']}（原始响应与列表记录已保留）",
        "",
        "## 已保存文件与旧版本证据",
        "",
        "文件字节校验通过不代表归档完整；完整性以 validation.ok 为准。",
        f"旧版本未补证资源：{assets.get('legacy_unverified', 0)}。文件可能仍存在；运行 verify --rebuild-legacy-evidence 从 raw 离线补证。",
        "",
        "## 资源",
        "",
        f"- 发现：{assets['discovered']}（其中可下载 {assets['downloadable']}，非 HTTP {assets['non_http']}）",
        f"- 尝试下载：{assets['attempted']}",
        f"- 成功：{assets['downloaded']}",
        f"- 失败：{assets['failed']}（原始 URL 已保留，未从 HTML 中删除）",
        "",
        "## 重复（仅报告，未做任何删除）",
        "",
        f"- 重复 key：{len(duplicates['duplicate_keys'])}",
        f"- 重复标题：{len(duplicates['duplicate_titles'])}",
        f"- 重复正文 hash：{len(duplicates['duplicate_content_hashes'])}",
        "",
    ]

    problems = summary["problem_items"]
    integrity = summary["stored_file_integrity"]
    lines += [
        "## 文件校验", "",
        f"- 已保存文件校验通过：{integrity['checked_files']} 个文件（不表示归档完整）",
        f"- 当前资源完整性：{tick(integrity['current_resource_integrity']['ok'])}",
        f"- 保留历史资源完整性：{tick(integrity['historical_retained_resource_integrity']['ok'])}",
        f"- 缺少历史摘要，暂不能验证：{integrity['unverified_files']} 个文件",
        f"- 损坏、缺失或派生版本过期：{integrity['issue_count']} 项",
        "- 明细见 `reports/stored_file_integrity.json`。", "",
    ]
    if problems:
        lines += [
            f"## ⚠️ 需要关注的条目：{problems} 条",
            "",
            "明细见 `reports/failed_items.csv`，可用 `baidu-archive retry-failed` 重试。",
            "",
        ]
    elif validation_result(summary)['ok']:
        lines += ["## ✅ 没有需要关注的条目", ""]
    else:
        lines += ["## ⚠️ 归档未通过完整性校验", "",
                  "请检查上面的索引数量、原始顺序与文件校验结果。", ""]

    return "\n".join(lines)
