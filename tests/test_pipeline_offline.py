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

from conftest import seed_generation

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

    seed_generation(state, [r['key'] for r in records], parsed['total_count'])
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


@pytest.mark.parametrize("body, status", [((Path(__file__).parent / 'fixtures' / 'tiny.png').read_bytes(), "complete"),
                                         (b"<html>gone</html>", "assets_partial")])
def test_resource_result_matches_metadata_and_database(seeded, body, status):
    import httpx
    from baidu_archive.assets import run_assets
    from baidu_archive.auth import Credentials
    from baidu_archive.client import ArchiveClient

    config, state, key = seeded
    config.delay = config.jitter = 0
    seed_generation(state, [r["key"] for r in state.iter_records()])
    run_parse(config, state)
    with ArchiveClient(config, Credentials()) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body)))
        stats = run_assets(config, state, client, only_keys=[key])
    metadata = json.loads((config.item_dir(key) / "metadata.json").read_text())
    assert metadata["status"] == state.get_item(key)["status"] == status
    assert stats["attempted"] > 0
    assert bool(stats["failed"]) == (status == "assets_partial")


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
    assert 'src="archive-data.js"' in index
    assert 'href="brand.css"' in index and 'src="icon.png"' in index
    brand = Path(__file__).parents[1] / "baidu_archive" / "ui"
    for filename in ("brand.css", "icon.png"):
        assert (site / filename).read_bytes() == (brand / filename).read_bytes()
    data = (site / "archive-data.js").read_text(encoding="utf-8")
    assert "示例文章标题" in data
    assert "2011" in data                       # 按年份筛选
    assert '"other": 1' in data                 # 其他收藏不消失
    assert '"pending": 1' in data
    assert (site / "style.css").exists()
    articles = list((site / "articles").iterdir())
    assert len(articles) == 1
    page = (articles[0] / "index.html").read_text(encoding="utf-8")
    assert "示例文章标题" in page and "返回目录" in page
    assert 'href="../../index.html"' in page
    assert 'href="../../style.css"' in page
    assert 'href="../../brand.css"' in page and 'href="../../icon.png"' in page


def test_export_site_copies_assets_for_portability(seeded):
    config, state, key = seeded
    run_parse(config, state)
    import httpx
    from baidu_archive.assets import run_assets
    from baidu_archive.auth import Credentials
    from baidu_archive.client import ArchiveClient
    body = (FIXTURES / 'tiny.png').read_bytes()
    config.delay = config.jitter = 0
    with ArchiveClient(config, Credentials()) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body, headers={'content-type': 'image/png'})))
        run_assets(config, state, client, only_keys=[key])
    downloaded = [a for a in state.assets_for(key) if a['download_status'] == 'ok']
    assert len(downloaded) == 3
    site = config.root.parent / "elsewhere" / "reading"
    export_site(config, state, site)
    article_dir = next((site / "articles").iterdir())
    for asset in downloaded:
        assert (article_dir / asset['local_path']).read_bytes() == body
        assert ('src="' + asset['local_path'] + '"') in (article_dir / 'index.html').read_text()


def test_export_site_inert_reading_copy_preserves_original(seeded):
    config, state, key = seeded
    run_parse(config, state)
    source = config.item_dir(key) / "content.original.html"
    raw = '<p onclick="alert(1)">历史正文</p><script>alert(1)</script><iframe src="https://example.com"></iframe>'
    source.write_text(raw)
    # 模拟升级前归档（没有 parsed 版本字段），仍支持原始正文作为阅读源。
    metadata_path = config.item_dir(key) / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.pop("parsed_sha256")
    metadata_path.write_text(json.dumps(metadata))
    site = config.root.parent / "site"
    export_site(config, state, site)
    article = next((site / "articles").glob("*/index.html")).read_text()
    assert "历史正文" in article
    assert "alert(1)" not in article and "<iframe" not in article
    assert source.read_text() == raw


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


def test_verify_rejects_duplicate_positions_even_without_gap(seeded):
    config, state, key = seeded
    records = state.iter_records()
    state.upsert_record({**records[1], "global_position": 0})
    assert run_verify(config, state)["original_order"]["contiguous"] is False


def test_verify_completed_items_keep_cumulative_progress(seeded):
    config, state, key = seeded
    run_parse(config, state)
    state.advance_status(key, "complete")
    summary = run_verify(config, state)
    assert summary["baidu_space_pages"]["parsed"] == 0
    assert summary["baidu_space_pages"]["stages"]["parsed"] == 1
    report = (config.reports_dir / "summary.md").read_text()
    assert "| 已解析 | 1 |" in report


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


def test_wxr_uses_remote_images_even_after_localization(seeded):
    from baidu_archive.assets import localize_html
    from baidu_archive.storage import current_content_path, write_json_atomic
    import xml.etree.ElementTree as ET
    config, state, key = seeded
    run_parse(config, state)
    folder = config.item_dir(key)
    original = current_content_path(folder, prefer_local=False).read_text()
    image = next(a for a in state.assets_for(key) if a['kind']=='image' and a['downloadable'])
    local = localize_html(original, {image['original_url']: 'assets/local.jpg'})
    (folder/'content.local.html').write_text(local)
    metadata = json.loads((folder/'metadata.json').read_text())
    metadata['localized_from_sha256'] = metadata['parsed_sha256']
    metadata['localized_resource_fingerprint'] = metadata['resource_fingerprint']
    write_json_atomic(folder/'metadata.json', metadata)
    assert 'assets/local.jpg' in current_content_path(folder).read_text()
    document = ET.parse(export_wxr(config, state))
    content = document.find('.//{http://purl.org/rss/1.0/modules/content/}encoded').text
    assert image['resolved_url'] in content
    assert 'src="assets/' not in content and 'srcset="assets/' not in content


@pytest.mark.parametrize('special', ['A&B','<tag>','"quoted"',"'apostrophe'",'A&B <C> "D" \'E\' ]]>'])
def test_wxr_xml_special_characters_round_trip(seeded, special):
    import xml.etree.ElementTree as ET
    from baidu_archive.storage import write_json_atomic
    config,state,key = seeded
    run_parse(config,state)
    folder = config.item_dir(key)
    metadata = json.loads((folder/'metadata.json').read_text())
    metadata.update(title=special, tags=[special])
    write_json_atomic(folder/'metadata.json',metadata)
    config.site_root = 'https://example.test/?a=1&b=<2>"\''
    document=ET.parse(export_wxr(config,state))
    assert document.find('channel/link').text == config.site_root
    assert document.find('.//item/title').text == special
    category=document.find('.//category[@domain="post_tag"]')
    assert category.text == special
    from baidu_archive.utils import sanitize_filename
    assert category.attrib['nicename'] == sanitize_filename(special,max_length=30)


def test_wxr_legacy_local_path_restored_and_unmapped_path_rejected():
    from baidu_archive.export import wxr_content
    from baidu_archive.storage import IntegrityError
    content='<img src="assets/local.png">'
    restored=wxr_content(content,{},[{'original_url':'https://x/original.png','local_path':'assets/local.png'}])
    assert 'src="https://x/original.png"' in restored and 'src="assets/' not in restored
    with pytest.raises(IntegrityError):
        wxr_content(content,{},[])


def test_wxr_relative_images_resolve_against_current_original_page():
    from baidu_archive.export import wxr_content
    restored=wxr_content('<img src="images/photo.png">',{'iframe_url_original':'https://x/articles/body.html'},[])
    assert 'src="https://x/articles/images/photo.png"' in restored


def test_wxr_invalid_xml_character_does_not_publish(seeded):
    from baidu_archive.storage import IntegrityError, write_json_atomic
    config,state,key=seeded
    run_parse(config,state)
    path=config.item_dir(key)/'metadata.json'
    metadata=json.loads(path.read_text())
    metadata['title']='invalid\x00title'
    write_json_atomic(path,metadata)
    with pytest.raises(IntegrityError):
        export_wxr(config,state)
    assert not (config.root/'wordpress_import.xml').exists()
