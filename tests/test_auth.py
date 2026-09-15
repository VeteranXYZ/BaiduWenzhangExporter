import pytest

from baidu_archive.auth import (
    AuthError,
    Credentials,
    _cookies_from_json,
    _cookies_from_netscape,
    build_list_url_template,
    load_credentials,
    parse_curl,
)

FAKE_CURL = """curl 'https://wenzhang.baidu.com/api/getlist?start=30&limit=30&tag=' \\
  -H 'Accept: application/json' \\
  -H 'Cookie: BDUSS=FAKEBDUSS; STOKEN=FAKESTOKEN' \\
  -H 'User-Agent: Mozilla/5.0 (Macintosh)' \\
  -H 'Referer: https://wenzhang.baidu.com/' \\
  --compressed"""


def test_parse_curl_extracts_everything():
    parsed = parse_curl(FAKE_CURL)
    assert parsed["url"].startswith("https://wenzhang.baidu.com/api/getlist")
    assert "BDUSS=FAKEBDUSS" in parsed["cookie_header"]
    assert parsed["headers"]["user-agent"] == "Mozilla/5.0 (Macintosh)"
    assert parsed["headers"]["referer"] == "https://wenzhang.baidu.com/"


def test_parse_curl_rejects_missing_url():
    with pytest.raises(AuthError):
        parse_curl("curl -H 'Cookie: a=b'")


def test_build_list_url_template_parameterizes_paging():
    template = build_list_url_template("https://x.com/api/getlist?start=30&limit=30&tag=")
    assert "{start}" in template and "{limit}" in template
    assert template.format(start=90, limit=30).count("start=90") == 1


def test_build_list_url_template_adds_missing_params():
    template = build_list_url_template("https://x.com/api/getlist?foo=1")
    assert "{start}" in template and "{limit}" in template
    assert "foo=1" in template


def test_build_list_url_template_handles_alternate_names():
    """有的接口用 pn/rn 而不是 start/limit。"""
    template = build_list_url_template("https://x.com/api?pn=0&rn=30")
    assert "{start}" in template and "{limit}" in template


def test_credentials_detect_login_state():
    assert Credentials(cookie_header="BDUSS=x; OTHER=y").looks_logged_in() is True
    assert Credentials(cookie_header="OTHER=y").looks_logged_in() is False
    assert Credentials().present is False


def test_cookies_from_json_array():
    out = _cookies_from_json('[{"name":"BDUSS","value":"V1"},{"name":"S","value":"V2"}]')
    assert out == "BDUSS=V1; S=V2"


def test_cookies_from_json_mapping():
    assert _cookies_from_json('{"BDUSS":"V1"}') == "BDUSS=V1"


def test_cookies_from_json_invalid():
    with pytest.raises(AuthError):
        _cookies_from_json("not json")


def test_cookies_from_netscape():
    text = (
        "# Netscape HTTP Cookie File\n"
        ".baidu.com\tTRUE\t/\tTRUE\t0\tBDUSS\tFAKEVALUE\n"
        "\n"
    )
    assert _cookies_from_netscape(text) == "BDUSS=FAKEVALUE"


def test_load_credentials_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BAIDU_ARCHIVE_COOKIE", "BDUSS=FROMENV")
    creds = load_credentials(secrets_dir=tmp_path)
    assert creds.cookie_header == "BDUSS=FROMENV"
    assert creds.source.startswith("env:")


def test_load_credentials_absent_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("BAIDU_ARCHIVE_COOKIE", raising=False)
    creds = load_credentials(secrets_dir=tmp_path)
    assert creds.present is False
    assert creds.source == "none"
