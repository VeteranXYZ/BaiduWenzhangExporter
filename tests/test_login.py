"""浏览器登录与凭证落盘。全部为脱敏构造数据，不含任何真实 Cookie。"""
import json
import stat
from http.cookies import SimpleCookie

import pytest

from baidu_archive.auth import AuthError, Credentials, load_credentials, save_cookie_session
from baidu_archive.login import cookie_header_from, default_list_url_template
from baidu_archive.config import Config


def _cookie(name, value):
    jar = SimpleCookie()
    jar[name] = value
    return jar


# ---------------- Cookie 拼接 ----------------

def test_cookie_header_joins_simple_cookies():
    out = cookie_header_from([_cookie("BDUSS", "FAKE1"), _cookie("STOKEN", "FAKE2")])
    assert out == "BDUSS=FAKE1; STOKEN=FAKE2"


def test_cookie_header_keeps_first_of_duplicates():
    out = cookie_header_from([_cookie("BDUSS", "FIRST"), _cookie("BDUSS", "SECOND")])
    assert out == "BDUSS=FIRST"


def test_cookie_header_tolerates_empty_and_pairs():
    assert cookie_header_from(None) == ""
    assert cookie_header_from([]) == ""
    assert cookie_header_from([("A", "1"), ("B", "2")]) == "A=1; B=2"


def test_login_helpers_import_without_optional_gui_dependency():
    import subprocess
    import sys

    script = '''
import sys
sys.modules['webview'] = None
from baidu_archive.login import cookie_header_from, default_list_url_template, open_login_window
from baidu_archive.config import Config
assert cookie_header_from([('BDUSS', 'FAKE')]) == 'BDUSS=FAKE'
assert '/fav/list' in default_list_url_template(Config())
try:
    open_login_window(Config())
except ModuleNotFoundError as exc:
    assert exc.name == 'webview'
else:
    raise AssertionError('GUI should require pywebview')
'''
    subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)


@pytest.mark.parametrize('current_url, trusted', [
    ('https://wenzhang.baidu.com/', True),
    ('https://wenzhang.baidu.com:443/', True),
    ('http://wenzhang.baidu.com/', False),
    ('https://wenzhang.baidu.com:8443/', False),
    ('https://example.test/', False),
])
def test_login_window_reads_cookies_only_on_expected_origin(monkeypatch, current_url, trusted):
    import sys
    from types import SimpleNamespace

    from baidu_archive import login

    class Event:
        def __iadd__(self, callback):
            self.callback = callback
            return self

    reads = []
    saved = []

    def cookies():
        reads.append(True)
        return [_cookie('BDUSS', 'FAKE')]

    window = SimpleNamespace(events=SimpleNamespace(loaded=Event(), closed=Event()),
                             get_current_url=lambda: current_url, get_cookies=cookies,
                             destroy=lambda: None)
    monkeypatch.setitem(sys.modules, 'webview', SimpleNamespace(create_window=lambda *a, **kw: window))
    monkeypatch.setattr(login, 'save_cookie_session', lambda *a, **kw: saved.append(True) or {})
    results = []
    login.open_login_window(Config(), on_result=results.append)
    window.events.loaded.callback()
    assert reads == saved == ([True] if trusted else [])
    assert results == ([{'ok': True}] if trusted else [])


# ---------------- 凭证落盘 ----------------

def test_save_cookie_session_rejects_without_bduss(tmp_path):
    """没有 BDUSS 就不是登录态，必须拒绝而不是存一份没用的凭证。"""
    with pytest.raises(AuthError):
        save_cookie_session("OTHER=x", secrets_dir=tmp_path)
    assert not (tmp_path / "session.json").exists()


def test_save_cookie_session_writes_private_file(tmp_path):
    summary = save_cookie_session("BDUSS=FAKE; STOKEN=FAKE2",
                                  list_url_template="https://x/fav/list?start={start}&limit={limit}",
                                  secrets_dir=tmp_path)
    path = tmp_path / "session.json"
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o077 == 0, f"会话文件不得对其他用户可读，当前 {oct(mode)}"
    assert summary["cookie_names"] == ["BDUSS", "STOKEN"]
    assert "FAKE" not in json.dumps(summary)        # 摘要只含名字，不含值


def test_save_cookie_session_preserves_existing_url_template(tmp_path):
    """重新登录不应把已配置的列表端点冲掉。"""
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "session.json").write_text(json.dumps({
        "list_url_template": "https://wenzhang.baidu.com/fav/list?start={start}&limit={limit}",
        "cookie_header": "BDUSS=OLD", "user_agent": "UA-OLD",
    }), encoding="utf-8")
    save_cookie_session("BDUSS=NEW", secrets_dir=tmp_path)
    saved = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
    assert saved["cookie_header"] == "BDUSS=NEW"
    assert "fav/list" in saved["list_url_template"]
    assert saved["user_agent"] == "UA-OLD"


def test_saved_session_is_loadable(tmp_path):
    save_cookie_session("BDUSS=FAKE", secrets_dir=tmp_path)
    creds = load_credentials(secrets_dir=tmp_path)
    assert creds.present and creds.looks_logged_in()


# ---------------- 端点回退 ----------------

def test_default_list_url_template_prefers_configured():
    cfg = Config(list_url_template="https://configured/fav/list?start={start}&limit={limit}")
    assert default_list_url_template(cfg).startswith("https://configured")


def test_default_list_url_template_falls_back_to_known_endpoint():
    """扫码拿不到列表 URL，但该端点已知，退回内置候选即可。"""
    template = default_list_url_template(Config())
    assert template and "/fav/list" in template
    assert "{start}" in template and "{limit}" in template


# ---------------- GUI 凭证接口 ----------------

def test_credential_state_never_returns_cookie_values(tmp_path, monkeypatch):
    import importlib, sys, types
    view = types.ModuleType("webview"); menu = types.ModuleType("webview.menu")
    menu.Menu = menu.MenuAction = menu.MenuSeparator = object
    monkeypatch.setitem(sys.modules, "webview", view)
    monkeypatch.setitem(sys.modules, "webview.menu", menu)
    gui = importlib.import_module("baidu_archive.gui")
    sys.modules.pop("baidu_archive.gui", None)

    monkeypatch.setenv("BAIDU_ARCHIVE_COOKIE", "BDUSS=SUPERSECRET; STOKEN=ALSOSECRET")
    state = gui.Api(Config(root=tmp_path / "archive")).credential_state()
    assert state["present"] and state["logged_in"]
    assert state["cookie_names"] == ["BDUSS", "STOKEN"]
    assert "SUPERSECRET" not in json.dumps(state, ensure_ascii=False)
    assert "ALSOSECRET" not in json.dumps(state, ensure_ascii=False)


def test_credentials_absent_reports_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("BAIDU_ARCHIVE_COOKIE", raising=False)
    creds = load_credentials(secrets_dir=tmp_path / "nope")
    assert not creds.present and not creds.looks_logged_in()
