"""离线全流程：只读 raw/，全程零网络请求。

这组测试对应验收标准 F/G/H/N —— 证明"即使解析器有 bug，也能从 raw 重新生成归档"。
"""

import json
from pathlib import Path

import pytest

from baidu_archive.config import Config
from baidu_archive.export import export_site, export_wxr, run_export
from baidu_archive.parsers import parse_list_html, parse_list_json
from baidu_archive.pipeline import export_master_index, run_parse
from baidu_archive.state import ArchiveState
from baidu_archive.verify import run_verify

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def seeded(tmp_path):
    """用 fixture 搭一个已完成 fetch 阶段的归档目录。"""
    config = Config(root=tmp_path / "archive")
    config.ensure_dirs()
    state = ArchiveState(config.db_path)

    parsed = parse_list_json((FIXTURES / "list_response.json").read_text(encoding="utf-8"))
    state.set_meta("total_count", parsed["total_count"])
    records = parse_list_html(parsed["html"], offset=0)
    for record in records:
        state.upsert_record(record)
        if record["type"] == "page":
            state.ensure_item(record["key"], "pending")
        else:
            state.update_item(record["key"], status="skipped_non_page")

    # 只给第一条 page 造 raw 数据，第二条 page 故意留空（模拟未抓取）
    key = records[0]["key"]
    (config.detail_dir / f"{key}.html").write_text(
        (FIXTURES / "detail_page.html").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (config.content_dir / f"{key}.html").write_text(
        (FIXTURES / "content_page.html").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state.advance_status(key, "content_fetched")
    yield config, state, key
    state.close()


def test_parse_runs_without_network(seeded):
    config, state, key = seeded
    stats = run_parse(config, state)
    assert stats["parsed"] == 1
    assert stats["missing_raw"] == 1        # 第二条 page 没有 raw


def test_parse_writes_original_html_unmodified(seeded):
    config, state, key = seeded
    run_parse(config, state)
    original = (config.item_dir(key) / "content.original.html").read_text(encoding="utf-8")
    assert "renren.com" in original
    assert "tudou.com" in original
    assert "lazy2.jpg" in original           # 懒加载属性未被吃掉
    assert "data:image/gif" in original      # data URI 原样保留


def test_parse_metadata_keeps_both_dates(seeded):
    """列表日期与正文 footer 日期都要保存，不擅自判断谁'正确'。"""
    config, state, key = seeded
    run_parse(config, state)
    metadata = json.loads((config.item_dir(key) / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["list_date"] == "2011-02-08"
    assert metadata["content_date"] == "2011-02-08"
    assert "list_date_text" in metadata and "content_date_text" in metadata


def test_parse_metadata_keeps_empty_fields(seeded):
    """字段为空也不删除 —— 结构稳定才能长期归档。"""
    config, state, key = seeded
    run_parse(config, state)
    metadata = json.loads((config.item_dir(key) / "metadata.json").read_text(encoding="utf-8"))
    for field in ("key", "type", "title", "tags", "source", "list_desc", "external_url",
                  "assets", "warnings", "errors", "global_position", "list_offset"):
        assert field in metadata


def test_parse_metadata_redacts_signed_url(seeded):
    config, state, key = seeded
    run_parse(config, state)
    metadata = json.loads((config.item_dir(key) / "metadata.json").read_text(encoding="utf-8"))
    assert "FAKESIGNVALUE" not in metadata["iframe_url_redacted"]


def test_parse_registers_assets(seeded):
    config, state, key = seeded
    run_parse(config, state)
    assets = state.assets_for(key)
    kinds = {a["kind"] for a in assets}
    assert "image" in kinds and "link" in kinds
    data_uris = [a for a in assets if a["scheme_kind"] == "data"]
    assert data_uris and data_uris[0]["downloadable"] == 0


def test_parse_is_idempotent_and_resumable(seeded):
    config, state, key = seeded
    run_parse(config, state)
    second = run_parse(config, state)
    assert second["skip"] == 1 and second["parsed"] == 0   # 已完成的直接跳过


def test_parse_force_rebuilds(seeded):
    config, state, key = seeded
    run_parse(config, state)
    again = run_parse(config, state, force=True)
    assert again["parsed"] == 1


def test_export_md_txt(seeded):
    config, state, key = seeded
    run_parse(config, state)
    run_export(config, state, formats=["md", "txt"])
    item_dir = config.item_dir(key)
    markdown = (item_dir / "content.md").read_text(encoding="utf-8")
    text = (item_dir / "content.txt").read_text(encoding="utf-8")
    assert "第一段正文" in markdown
    assert "人人网主页" in text


def test_export_wxr_escapes_cdata(seeded):
    config, state, key = seeded
    run_parse(config, state)
    path = export_wxr(config, state)
    xml = path.read_text(encoding="utf-8")
    assert "<wp:wxr_version>1.2</wp:wxr_version>" in xml
    assert "示例文章标题" in xml
    assert "_archive_key" in xml
    # 原脚本用 ]]&gt; 会破坏正文；正确做法是拆分 CDATA
    assert "]]&gt;" not in xml


def test_export_site_is_browsable_offline(seeded):
    config, state, key = seeded
    run_parse(config, state)
    site = config.root.parent / "site"
    count = export_site(config, state, site)
    assert count == 1
    index = (site / "index.html").read_text(encoding="utf-8")
    assert "示例文章标题" in index
    assert "2011" in index                       # 按年份分组
    assert (site / "style.css").exists()
    articles = list((site / "articles").iterdir())
    assert len(articles) == 1
    page = (articles[0] / "index.html").read_text(encoding="utf-8")
    assert "示例文章标题" in page and "返回目录" in page


def test_master_index_preserves_original_order(seeded):
    config, state, key = seeded
    export_master_index(config, state)
    index = json.loads((config.root / "master_index.json").read_text(encoding="utf-8"))
    assert [e["global_position"] for e in index] == [0, 1, 2]
    assert index[0]["key"] == key
    csv_text = (config.root / "master_index.csv").read_text(encoding="utf-8")
    assert csv_text.startswith("﻿")          # Excel 友好
    assert "完整版本" in csv_text
    assert "来自于百度空间" in csv_text   # list_source 也要进 CSV


def test_master_index_includes_non_page_types(seeded):
    """504 条全部入索引，article 不许被丢弃。"""
    config, state, key = seeded
    export_master_index(config, state)
    index = json.loads((config.root / "master_index.json").read_text(encoding="utf-8"))
    types = {e["type"] for e in index}
    assert types == {"page", "article"}
    article = next(e for e in index if e["type"] == "article")
    assert article["external_url"] == "https://zhidao.baidu.com/question/12345.html"


def test_verify_reports_gap_against_api_total(seeded):
    config, state, key = seeded
    run_parse(config, state)
    summary = run_verify(config, state)
    assert summary["api_total_count"] == 504
    assert summary["indexed"] == 3
    assert summary["index_missing_vs_api"] == 501     # 明确告诉用户漏了多少
    assert summary["index_complete"] is False
    assert summary["original_order"]["contiguous"] is True
    assert (config.reports_dir / "summary.md").exists()
    assert (config.reports_dir / "failed_items.csv").exists()
    assert (config.reports_dir / "external_resources.csv").exists()


def test_verify_external_resources_lists_dead_links(seeded):
    config, state, key = seeded
    run_parse(config, state)
    run_verify(config, state)
    csv_text = (config.reports_dir / "external_resources.csv").read_text(encoding="utf-8")
    assert "renren.com" in csv_text
    assert "tudou.com" in csv_text


def test_verify_status_counts_are_mutually_exclusive(seeded):
    """回归：曾经 status=='parsed' 被时间戳计数与状态计数重复累加，
    导致「已解析 4 / 总计 2」这种不可能的数字。"""
    config, state, key = seeded
    run_parse(config, state)
    summary = run_verify(config, state)
    pages = summary["baidu_space_pages"]
    exclusive = ["complete", "warning", "assets_partial", "parsed",
                 "failed", "auth_error", "pending", "detail_fetched", "content_fetched"]
    total_status = sum(pages.get(name, 0) for name in exclusive)
    assert total_status == pages["total"], f"状态分布合计 {total_status} != 总计 {pages['total']}"
    assert pages["parsed"] <= pages["total"]


def test_metadata_keeps_both_tag_sources_and_note(seeded):
    """列表标签与正文标签各存一份，备注也要保留。"""
    config, state, key = seeded
    run_parse(config, state)
    metadata = json.loads((config.item_dir(key) / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["tags"] == ["空间"]                      # 来自列表
    assert metadata["content_tags"] == ["空间", "旧文"]       # 来自正文页
    assert metadata["note"] == "这是收藏时写的备注"
    assert metadata["content_base_href"] == "http://1"       # 记录但不采信


def test_metadata_keeps_both_sources(seeded):
    config, state, key = seeded
    run_parse(config, state)
    metadata = json.loads((config.item_dir(key) / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["list_source"] == "来自于百度空间"
    assert metadata["content_source"] == "来自于百度空间"
