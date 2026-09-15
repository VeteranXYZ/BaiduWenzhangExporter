import pytest

from baidu_archive.utils import (
    domain_of,
    extension_for,
    readable_dirname,
    redact_cookie,
    redact_url,
    safe_key_dirname,
    sanitize_filename,
    sha256_bytes,
    url_scheme_kind,
)


# ---------------- URL 脱敏 ----------------

def test_redact_url_hides_sign_and_expire():
    url = "https://x.com/page/content/?uid=000000000&key=K1&is_share=0&expire=1789504567&sign=SECRETVALUE"
    out = redact_url(url)
    assert "SECRETVALUE" not in out
    assert "000000000" not in out
    assert "1789504567" not in out
    assert "key=K1" in out          # 非敏感参数必须保留，否则报告失去可读性
    assert "is_share=0" in out


def test_redact_url_without_query():
    assert redact_url("https://x.com/a/b") == "https://x.com/a/b"


def test_redact_url_empty():
    assert redact_url("") == ""


def test_redact_url_keeps_blank_values():
    assert "empty=" in redact_url("https://x.com/?empty=&sign=S")


def test_redact_cookie_only_exposes_names():
    out = redact_cookie("BDUSS=SECRET1; STOKEN=SECRET2")
    assert "SECRET1" not in out and "SECRET2" not in out
    assert "BDUSS=<redacted>" in out and "STOKEN=<redacted>" in out


# ---------------- 文件名 ----------------

@pytest.mark.parametrize(
    "raw",
    ["我的/文章", "a:b*c?d", 'quote"here', "back\\slash", "pipe|char", "ctrl\x01char"],
)
def test_sanitize_filename_removes_illegal(raw):
    out = sanitize_filename(raw)
    assert not any(c in out for c in '<>:"/\\|?*')
    assert "\x01" not in out


def test_sanitize_filename_windows_reserved():
    assert sanitize_filename("CON").startswith("_")
    assert sanitize_filename("com1.txt").startswith("_")


def test_sanitize_filename_long_chinese_within_byte_limit():
    out = sanitize_filename("中" * 300)
    assert len(out.encode("utf-8")) <= 200


def test_sanitize_filename_empty_fallback():
    assert sanitize_filename("") == "untitled"
    assert sanitize_filename("...") == "untitled"


def test_safe_key_dirname_normal_key():
    assert safe_key_dirname("0000000000000002-1000000002") == "0000000000000002-1000000002"


def test_safe_key_dirname_hostile_key():
    out = safe_key_dirname("../../etc/passwd")
    assert "/" not in out and ".." not in out.strip(".")


def test_readable_dirname():
    out = readable_dirname("2011-02-08", "示例文章标题", "aaaabbbb-111")
    assert out.startswith("2011-02-08_")
    assert "示例文章标题" in out


# ---------------- 扩展名 ----------------

@pytest.mark.parametrize(
    "content_type,url,expected",
    [
        ("image/jpeg", None, ".jpg"),
        ("image/png", None, ".png"),
        ("image/gif; charset=binary", None, ".gif"),
        ("image/webp", None, ".webp"),
        (None, "http://x/a.PNG", ".png"),
        (None, "http://x/a.jpg?v=2", ".jpg"),
        (None, "http://x/noext", ".bin"),
        (None, None, ".bin"),
        ("application/x-shockwave-flash", None, ".swf"),
    ],
)
def test_extension_for(content_type, url, expected):
    assert extension_for(content_type, url) == expected


# ---------------- URL 分类 ----------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://a.com/x", "http"),
        ("https://a.com/x", "http"),
        ("//a.com/x", "protocol-relative"),
        ("data:image/gif;base64,AAA", "data"),
        ("javascript:void(0)", "javascript"),
        ("about:blank", "about"),
        ("mailto:a@b.c", "mailto"),
        ("/relative/path", "relative"),
        ("", "empty"),
    ],
)
def test_url_scheme_kind(url, expected):
    assert url_scheme_kind(url) == expected


def test_domain_of():
    assert domain_of("http://WWW.Renren.com/p") == "www.renren.com"
    assert domain_of("not a url") == ""


def test_sha256_is_stable():
    assert sha256_bytes(b"abc") == sha256_bytes(b"abc")
    assert sha256_bytes(b"abc") != sha256_bytes(b"abd")
