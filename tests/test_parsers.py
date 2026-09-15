"""解析器单测。fixture 均为脱敏构造数据，不含任何真实凭证。"""

import pytest

from baidu_archive.parsers import (
    ListResponseError,
    extract_assets,
    looks_like_login_page,
    normalize_date,
    parse_content_html,
    parse_detail_html,
    parse_list_html,
    parse_list_json,
)


# ---------------- 列表 JSON ----------------

def test_parse_list_json(list_response):
    parsed = parse_list_json(list_response)
    assert parsed["total_count"] == 504
    assert parsed["limit"] == 30
    assert parsed["start"] == 0
    assert parsed["current_count"] == 3
    assert parsed["status"] == 0
    assert "<li" in parsed["html"]


def test_parse_list_json_rejects_non_json():
    with pytest.raises(ListResponseError):
        parse_list_json("<html>login</html>")


def test_parse_list_json_rejects_missing_data():
    with pytest.raises(ListResponseError):
        parse_list_json('{"status":-1,"msg":"未登录"}')


# ---------------- 列表 HTML ----------------

def test_parse_list_html_types_and_keys(list_response):
    parsed = parse_list_json(list_response)
    records = parse_list_html(parsed["html"], offset=0)
    assert len(records) == 3
    assert [r["type"] for r in records] == ["page", "article", "page"]
    assert records[0]["key"] == "aaaabbbbccccdddd-1111111111"
    assert records[1]["key"] == "eeeeffff00001111-2222222222"


def test_parse_list_html_preserves_original_order(list_response):
    parsed = parse_list_json(list_response)
    records = parse_list_html(parsed["html"], offset=90)
    assert [r["position_in_page"] for r in records] == [0, 1, 2]
    assert [r["global_position"] for r in records] == [90, 91, 92]
    assert all(r["list_offset"] == 90 for r in records)


def test_parse_list_html_keeps_raw_html(list_response):
    """raw_html 必须保留，否则解析器改进后无法离线重建。"""
    parsed = parse_list_json(list_response)
    records = parse_list_html(parsed["html"], offset=0)
    assert all(r["raw_html"].startswith("<li") for r in records)


def test_parse_list_html_extracts_metadata(list_response):
    parsed = parse_list_json(list_response)
    records = parse_list_html(parsed["html"], offset=0)
    first = records[0]
    assert first["list_date"] == "2011-02-08"
    assert first["tags"] == ["空间"]
    assert "截断" in first["list_desc"]
    assert first["detail_url"].endswith("/page/view?key=aaaabbbbccccdddd-1111111111")


def test_parse_list_html_prefers_untruncated_title(list_response):
    """回归：列表项里标题出现两次，.unit-c 里的 a._photoLink 是缩略版。
    早期实现取了缩略版，导致标题被静默截断。"""
    parsed = parse_list_json(list_response)
    first = parse_list_html(parsed["html"], offset=0)[0]
    assert first["title"] == "关于旧文标题的完整版本，这一段在缩略版里会被截断"
    assert not first["title"].endswith("这一段在")


def test_parse_list_html_tags_come_from_spans(list_response):
    """回归：真实结构是 <div class="tags"><span>…</span></div>，
    早期只选了 a 标签，结果标签全部丢失。"""
    parsed = parse_list_json(list_response)
    records = parse_list_html(parsed["html"], offset=0)
    assert records[0]["tags"] == ["空间"]
    assert records[1]["tags"] == ["收藏"]
    assert records[2]["tags"] == []


def test_parse_list_html_tags_exclude_action_buttons(list_response):
    """「编辑」「删除」按钮同样用 span，绝不能混进标签。"""
    parsed = parse_list_json(list_response)
    for record in parse_list_html(parsed["html"], offset=0):
        assert "编辑" not in record["tags"]
        assert "删除" not in record["tags"]


def test_parse_list_html_captures_list_source(list_response):
    """.page-url 在列表层就给出来源，与正文 footer 的来源各存一份。"""
    parsed = parse_list_json(list_response)
    records = parse_list_html(parsed["html"], offset=0)
    assert records[0]["list_source"] == "来自于百度空间"
    assert records[1]["list_source"] == "来自于百度知道"


def test_parse_list_html_ignores_placeholder_external_url(list_response):
    """回归：百度空间条目的外层 h4 带 href="http://1" 占位符，
    早期被当成真实外链记录进档案。"""
    parsed = parse_list_json(list_response)
    records = parse_list_html(parsed["html"], offset=0)
    assert records[0]["external_url"] is None
    assert records[2]["external_url"] is None


def test_parse_list_html_external_url(list_response):
    parsed = parse_list_json(list_response)
    records = parse_list_html(parsed["html"], offset=0)
    assert records[1]["external_url"] == "https://zhidao.baidu.com/question/12345.html"


def test_parse_list_html_missing_date_is_none_not_fabricated(list_response):
    """没有日期就是 None，绝不编造 1970-01-01。"""
    parsed = parse_list_json(list_response)
    records = parse_list_html(parsed["html"], offset=0)
    assert records[2]["list_date"] is None


# ---------------- 日期 ----------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("收藏于 2011-02-08", "2011-02-08"),
        ("2009年03月05日", "2009-03-05"),
        ("2008.12.1", "2008-12-01"),
        ("2011/2/8", "2011-02-08"),
        ("无日期", None),
        ("", None),
        (None, None),
        ("9999年99月99日", None),
    ],
)
def test_normalize_date(text, expected):
    assert normalize_date(text) == expected


# ---------------- 详情页 ----------------

def test_parse_detail_decodes_html_entities(detail_page):
    """&amp; 必须解码成 & —— 否则签名 URL 会带字面 &amp; 而失效。"""
    info = parse_detail_html(detail_page)
    assert info["has_iframe"] is True
    assert "&amp;" not in info["iframe_url"]
    assert "sign=FAKESIGNVALUE" in info["iframe_url"]
    assert info["iframe_url"].startswith("https://wenzhang.bdstatic.com/page/content/")


def test_parse_detail_extracts_bdstoken(detail_page):
    assert parse_detail_html(detail_page)["bdstoken"] == "FAKE0000TOKEN0000FAKE"


def test_parse_detail_without_iframe():
    info = parse_detail_html("<html><body>没有 iframe</body></html>")
    assert info["has_iframe"] is False
    assert info["iframe_url"] is None


def test_parse_detail_protocol_relative_iframe():
    info = parse_detail_html('<iframe class="pcs-article-iframe" src="//example.com/page/content/?a=1">')
    assert info["iframe_url"].startswith("https://")


# ---------------- 正文页 ----------------

def test_parse_content_uses_prefix_not_hardcoded_hash(content_page):
    """fixture 里的哈希段是 zzzzhashzzzz，与真实值不同 —— 前缀匹配必须仍然命中。"""
    parsed = parse_content_html(content_page)
    assert parsed["has_container"] is True
    assert parsed["title"] == "示例文章标题"
    assert parsed["content_date"] == "2011-02-08"
    assert parsed["source"] == "来自于百度空间"


def test_parse_content_preserves_raw_html(content_page):
    """正文必须是原始 HTML，实体与标签都不许被吃掉。"""
    parsed = parse_content_html(content_page)
    html = parsed["content_html"]
    assert "<p>" in html and "<img" in html
    assert "renren.com" in html
    assert "&nbsp;" in html or "\xa0" in html


def test_parse_content_missing_container():
    parsed = parse_content_html("<html><body><p>没有容器</p></body></html>")
    assert parsed["has_container"] is False
    assert parsed["content_html"] is None
    assert parsed["content_length"] == 0


# ---------------- 资源提取 ----------------

def test_extract_assets_finds_all_kinds(content_page):
    content = parse_content_html(content_page)["content_html"]
    assets = extract_assets(content, base_url="https://wenzhang.bdstatic.com/page/content/")
    kinds = {a["kind"] for a in assets}
    assert "image" in kinds and "link" in kinds and "embed" in kinds


def test_extract_assets_handles_lazy_data_src(content_page):
    """老页面真实地址常在 data-src，漏了就等于丢图。"""
    content = parse_content_html(content_page)["content_html"]
    assets = extract_assets(content)
    lazy = [a for a in assets if a["attribute"] == "data-src"]
    assert len(lazy) == 1
    assert "lazy2.jpg" in lazy[0]["original_url"]


def test_extract_assets_classifies_data_uri(content_page):
    """data: 必须被分类而不是当成 HTTP 下载。"""
    content = parse_content_html(content_page)["content_html"]
    assets = extract_assets(content)
    data_uris = [a for a in assets if a["scheme_kind"] == "data"]
    assert len(data_uris) == 1
    assert data_uris[0]["downloadable"] is False
    assert data_uris[0]["resolved_url"] is None


def test_extract_assets_keeps_external_links(content_page):
    content = parse_content_html(content_page)["content_html"]
    assets = extract_assets(content)
    domains = {a["domain"] for a in assets if a["domain"]}
    assert "www.renren.com" in domains
    assert "www.tudou.com" in domains


def test_extract_assets_empty_input():
    assert extract_assets("") == []
    assert extract_assets(None) == []


# ---------------- 登录页识别 ----------------

def test_looks_like_login_page(login_page, content_page):
    assert looks_like_login_page(login_page) is True
    assert looks_like_login_page(content_page) is False
    assert looks_like_login_page("") is False
