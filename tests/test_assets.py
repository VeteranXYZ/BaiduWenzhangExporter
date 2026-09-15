from baidu_archive.assets import _asset_filename, localize_html


def test_localize_rewrites_downloaded_only():
    html = ('<p><img src="http://ok.com/a.jpg">'
            '<img src="http://dead.com/b.jpg"></p>')
    out = localize_html(html, {"http://ok.com/a.jpg": "assets/0001_aa.jpg"})
    assert 'src="assets/0001_aa.jpg"' in out
    assert "http://dead.com/b.jpg" in out          # 失败的保持原样
    assert 'data-archive-original-src="http://ok.com/a.jpg"' in out


def test_localize_never_removes_failed_elements():
    """下载失败不得删除元素 —— 失效的 URL 本身就是档案信息。"""
    html = '<p><img src="http://dead1.com/a.jpg"><img src="http://dead2.com/b.jpg"></p>'
    out = localize_html(html, {})
    assert out.count("<img") == 2
    assert "dead1.com" in out and "dead2.com" in out


def test_localize_keeps_external_links_untouched():
    html = '<p><a href="http://www.renren.com/x">人人</a></p>'
    assert "renren.com" in localize_html(html, {"http://www.renren.com/x": "assets/x"})


def test_localize_handles_lazy_attributes():
    html = '<img data-src="http://ok.com/lazy.jpg">'
    out = localize_html(html, {"http://ok.com/lazy.jpg": "assets/0002_bb.jpg"})
    assert "assets/0002_bb.jpg" in out
    assert "http://ok.com/lazy.jpg" in out          # 原值仍可追溯


def test_localize_returns_fragment_not_document():
    out = localize_html("<p>hi</p>", {})
    assert "<html>" not in out and "<body>" not in out


def test_localize_empty_input():
    assert localize_html("", {}) == ""


def test_localize_drops_srcset_only_when_localized():
    html = '<img src="http://ok.com/a.jpg" srcset="http://ok.com/a2.jpg 2x">'
    out = localize_html(html, {"http://ok.com/a.jpg": "assets/0001_aa.jpg"})
    assert "srcset=" not in out or "data-archive-original-srcset" in out
    assert "data-archive-original-srcset" in out    # 原值留档


def test_asset_filename_contains_order_and_hash():
    name = _asset_filename(7, "abcdef1234567890", ".jpg")
    assert name == "0007_abcdef123456.jpg"


def test_asset_filename_differs_for_different_content():
    """不同内容绝不共用文件名，避免互相覆盖。"""
    assert _asset_filename(1, "a" * 64, ".jpg") != _asset_filename(1, "b" * 64, ".jpg")
