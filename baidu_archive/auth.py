"""凭证加载。源码中不存在任何真实 Cookie / bdstoken / sign。

加载优先级：
1. 环境变量 ``BAIDU_ARCHIVE_COOKIE``
2. ``secrets/session.json``（由 ``import-curl`` 生成，同时携带真实列表 URL）
3. ``secrets/cookies.txt`` / ``cookies.json`` / ``cookies.netscape.txt``
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from urllib.request import Request

SECRETS_DIR = Path("secrets")
SESSION_FILE = SECRETS_DIR / "session.json"
COOKIE_TXT = SECRETS_DIR / "cookies.txt"
COOKIE_JSON = SECRETS_DIR / "cookies.json"
COOKIE_NETSCAPE = SECRETS_DIR / "cookies.netscape.txt"

# 百度登录态的核心 cookie；缺少 BDUSS 基本可以断定未登录。
REQUIRED_COOKIE_HINTS = ("BDUSS",)

# 从 cURL 里原样保留这些头，其余丢弃（避免把 content-length 之类带进来）
_KEEP_HEADERS = {
    "user-agent",
    "referer",
    "accept",
    "accept-language",
    "x-requested-with",
    "origin",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
}


class AuthError(RuntimeError):
    """凭证缺失或失效。"""


@dataclass
class Credentials:
    cookie_header: str = ""
    user_agent: str | None = None
    referer: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    source: str = "none"
    cookie_jar: CookieJar | None = None

    @property
    def present(self) -> bool:
        return self.available_for('https://wenzhang.baidu.com/fav/list')

    def available_for(self, url: str) -> bool:
        return bool(self.header_for(url))

    def header_for(self, url: str) -> str:
        if self.cookie_jar is None:
            return self.cookie_header
        request = Request(url)
        self.cookie_jar.add_cookie_header(request)
        return request.get_header('Cookie') or ''

    def cookie_names(self, url: str = "https://wenzhang.baidu.com/fav/list") -> list[str]:
        names = []
        for chunk in self.header_for(url).split(";"):
            name, _, _ = chunk.strip().partition("=")
            if name:
                names.append(name)
        return names

    def looks_logged_in(self) -> bool:
        names = {n.upper() for n in self.cookie_names()}
        return any(hint.upper() in names for hint in REQUIRED_COOKIE_HINTS)


def _cookies_from_json(text: str) -> str:
    """浏览器扩展（EditThisCookie / Cookie-Editor）导出的 JSON 数组。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuthError(f"cookies.json 解析失败: {exc}") from exc
    if isinstance(data, dict):
        if "cookies" in data and isinstance(data["cookies"], list):
            data = data["cookies"]
        else:  # {name: value} 简单映射
            return "; ".join(f"{k}={v}" for k, v in data.items() if k)
    if not isinstance(data, list):
        raise AuthError("cookies.json 结构无法识别（应为数组或对象）")
    parts = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("name"):
            parts.append(f"{entry['name']}={entry.get('value','')}")
    return "; ".join(parts)


def parse_netscape_cookies(text: str) -> CookieJar:
    """Keep all seven fields; stdlib CookieJar applies domain/path/expiry/security.

    DomainStrictNonDomain also enforces the Netscape FALSE (host-only) flag.
    Malformed rows are ignored; ordinary comments never become cookies.
    """
    jar = CookieJar(policy=DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    for line in text.splitlines():
        line = line.lstrip(' ')
        http_only = line.startswith('#HttpOnly_')
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, flag, path, secure, expiry, name, value = fields
        if (flag not in {'TRUE', 'FALSE'} or secure not in {'TRUE', 'FALSE'} or
                not re.fullmatch(r'\.?[A-Za-z0-9.-]+', domain) or not path.startswith('/') or
                not re.fullmatch(r'[!#$%&\x27*+.^_`|~0-9A-Za-z-]+', name) or
                any(ord(c) < 32 or ord(c) > 126 or c == ';' for c in value)):
            continue
        try:
            expires = int(expiry)
        except ValueError:
            continue
        if expires < 0:
            continue
        domain = domain if flag == 'TRUE' else domain.lstrip('.')
        jar.set_cookie(Cookie(0, name, value, None, False, domain, flag == 'TRUE',
                              domain.startswith('.'), path, True, secure == 'TRUE',
                              expires or None, expires == 0, None, None,
                              {'HttpOnly': None} if http_only else {}, False))
    return jar


def _cookies_from_netscape(text: str) -> str:
    """Compatibility helper: return only cookies valid for the collection URL."""
    return Credentials(cookie_jar=parse_netscape_cookies(text)).header_for('https://wenzhang.baidu.com/')


def parse_curl(curl_text: str) -> dict:
    """解析 DevTools「Copy as cURL」，提取 URL、Cookie 与请求头。

    这是本项目推荐的凭证/端点获取方式：它让"列表接口到底是什么 URL"
    从猜测变成由真实请求确定。
    """
    # 去掉 shell 续行符，压成单行再交给 shlex
    cleaned = re.sub(r"\\\r?\n", " ", curl_text.strip())
    cleaned = re.sub(r"\s+\^\r?\n", " ", cleaned)  # Windows cmd 风格
    try:
        tokens = shlex.split(cleaned)
    except ValueError as exc:
        raise AuthError(f"cURL 命令解析失败: {exc}") from exc

    url = None
    headers: dict[str, str] = {}
    cookie_header = ""

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"curl", "--compressed", "--location", "-L", "-s", "--silent", "-k", "--insecure", "-i"}:
            index += 1
            continue
        if token in {"-H", "--header"} and index + 1 < len(tokens):
            name, _, value = tokens[index + 1].partition(":")
            name = name.strip().lower()
            value = value.strip()
            if name == "cookie":
                cookie_header = value
            elif name in _KEEP_HEADERS:
                headers[name] = value
            index += 2
            continue
        if token in {"-b", "--cookie"} and index + 1 < len(tokens):
            cookie_header = tokens[index + 1]
            index += 2
            continue
        if token in {"-X", "--request", "-A", "--user-agent", "-e", "--referer", "--data", "-d", "--data-raw"}:
            if token in {"-A", "--user-agent"} and index + 1 < len(tokens):
                headers["user-agent"] = tokens[index + 1]
            elif token in {"-e", "--referer"} and index + 1 < len(tokens):
                headers["referer"] = tokens[index + 1]
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if url is None and ("://" in token or token.startswith("//")):
            url = token
        index += 1

    if not url:
        raise AuthError("cURL 中未找到 URL")
    return {"url": url, "cookie_header": cookie_header, "headers": headers}


def build_list_url_template(url: str) -> str:
    """把捕获到的真实列表 URL 变成 ``{start}`` / ``{limit}`` 模板。"""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parts = urlparse(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not pairs:
        return url
    rebuilt = []
    saw_start = saw_limit = False
    for name, value in pairs:
        lowered = name.lower()
        if lowered in {"start", "offset", "pn", "page_start"}:
            rebuilt.append((name, "__START__"))
            saw_start = True
        elif lowered in {"limit", "rn", "num", "count", "page_size"}:
            rebuilt.append((name, "__LIMIT__"))
            saw_limit = True
        elif lowered in {"_", "_t", "t", "timestamp", "rand", "r"} and value.isdigit():
            # 缓存穿透参数：冻结成抓取当时的值会让 17 个分页请求带同一个"随机"值，
            # 既可能命中缓存也显得可疑。改成每次请求现取。
            rebuilt.append((name, "__TS__"))
        else:
            rebuilt.append((name, value))
    if not saw_start:
        rebuilt.append(("start", "__START__"))
    if not saw_limit:
        rebuilt.append(("limit", "__LIMIT__"))
    query = urlencode(rebuilt)
    template = urlunparse(parts._replace(query=query))
    return (
        template.replace("__START__", "{start}")
        .replace("__LIMIT__", "{limit}")
        .replace("__TS__", "{ts}")
    )


def import_curl(curl_text: str, *, secrets_dir: Path = SECRETS_DIR) -> dict:
    """解析 cURL 并写入 ``secrets/session.json``（权限 0600）。返回脱敏摘要。"""
    parsed = parse_curl(curl_text)
    secrets_dir.mkdir(parents=True, exist_ok=True)

    session = {
        "list_url_template": build_list_url_template(parsed["url"]),
        "cookie_header": parsed["cookie_header"],
        "user_agent": parsed["headers"].get("user-agent"),
        "referer": parsed["headers"].get("referer"),
        "extra_headers": {k: v for k, v in parsed["headers"].items() if k not in {"user-agent", "referer"}},
    }
    path = secrets_dir / "session.json"
    from .storage import write_json_atomic

    # mkstemp 创建的临时文件权限为 0600；原子替换也能安全更新已有会话。
    write_json_atomic(path, session)

    from .utils import redact_url

    creds = Credentials(cookie_header=parsed["cookie_header"])
    return {
        "path": str(path),
        "list_url_template_redacted": redact_url(session["list_url_template"]),
        "cookie_names": creds.cookie_names(),
        "looks_logged_in": creds.looks_logged_in(),
        "user_agent_present": bool(session["user_agent"]),
    }


def save_cookie_session(
    cookie_header: str,
    *,
    list_url_template: str | None = None,
    user_agent: str | None = None,
    referer: str | None = None,
    secrets_dir: Path = SECRETS_DIR,
) -> dict:
    """把已拿到的 Cookie 写成会话文件（权限 0600）。返回脱敏摘要。

    扫码登录走这条路：登录本身在浏览器窗口里由用户完成，程序只拿到结果
    Cookie。与 import_curl 共用同一份落盘逻辑，凭证写入只有这一处。
    """
    cookie_header = (cookie_header or "").strip()
    creds = Credentials(cookie_header=cookie_header)
    if not creds.looks_logged_in():
        raise AuthError("Cookie 中没有 BDUSS，不是有效的登录态")

    secrets_dir.mkdir(parents=True, exist_ok=True)
    existing = {}
    path = secrets_dir / "session.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}

    session = {
        # 列表端点已知且固定，扫码拿不到它也无妨；沿用已有配置优先。
        "list_url_template": list_url_template or existing.get("list_url_template"),
        "cookie_header": cookie_header,
        "user_agent": user_agent or existing.get("user_agent"),
        "referer": referer or existing.get("referer"),
        "extra_headers": existing.get("extra_headers") or {},
    }
    from .storage import write_json_atomic

    write_json_atomic(path, session)

    from .utils import redact_url

    return {
        "path": str(path),
        "cookie_names": creds.cookie_names(),
        "looks_logged_in": True,
        "list_url_template_redacted": redact_url(session["list_url_template"] or ""),
    }


def load_credentials(secrets_dir: Path = SECRETS_DIR) -> Credentials:
    """按优先级加载凭证。全部缺失时返回 ``present=False`` 的空凭证，不抛错。"""
    env_cookie = os.environ.get("BAIDU_ARCHIVE_COOKIE", "").strip()
    if env_cookie:
        return Credentials(cookie_header=env_cookie, source="env:BAIDU_ARCHIVE_COOKIE")

    session_path = secrets_dir / "session.json"
    if session_path.exists():
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthError(f"{session_path} 解析失败: {exc}") from exc
        if session.get("cookie_header"):
            return Credentials(
                cookie_header=session["cookie_header"],
                user_agent=session.get("user_agent"),
                referer=session.get("referer"),
                extra_headers=session.get("extra_headers") or {},
                source=str(session_path),
            )

    for path, loader in (
        (secrets_dir / "cookies.txt", lambda t: t.strip()),
        (secrets_dir / "cookies.json", _cookies_from_json),
        (secrets_dir / "cookies.netscape.txt", None),
    ):
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if loader is None:
                return Credentials(cookie_jar=parse_netscape_cookies(text), source=str(path))
            cookie = loader(text)
            if cookie:
                return Credentials(cookie_header=cookie, source=str(path))

    return Credentials(source="none")
