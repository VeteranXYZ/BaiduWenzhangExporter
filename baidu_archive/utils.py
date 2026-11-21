"""无副作用的纯工具函数。可独立单测，不依赖网络或配置。"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# query 中一旦出现这些参数名，值就必须在日志/报告里打码。
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "sign",
        "signature",
        "expire",
        "expires",
        "token",
        "bdstoken",
        "access_token",
        "secret",
        "sig",
        "uid",
        "passwd",
        "password",
        "sessionid",
        "bduss",
        "stoken",
        "ptoken",
    }
)

# Windows 保留设备名，作为目录名会直接失败。
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RUN = re.compile(r"\s+")


def utcnow_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（秒精度，带 Z）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decode_response_bytes(content: bytes, headers: dict[str, str] | None = None) -> str:
    """联网与离线解析使用相同编码规则，原始字节始终另存。"""
    encoding = None
    content_type = (headers or {}).get("content-type", "")
    match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", content_type, re.I)
    if match:
        encoding = match.group(1)
    for candidate in (encoding, "utf-8", "gb18030"):
        if candidate:
            try:
                return content.decode(candidate)
            except (UnicodeDecodeError, LookupError):
                pass
    return content.decode("utf-8", "replace")


def redact_url(url: str, extra_keys: Iterable[str] = ()) -> str:
    """把 URL 中的敏感 query 值替换为 ``<redacted>``。

    用于一切会被人看到的输出：终端日志、logfile、报告、异常信息。
    完整 URL 只允许出现在本地 metadata 中（用于将来复原）。
    """
    if not url:
        return url
    keys = SENSITIVE_QUERY_KEYS | {k.lower() for k in extra_keys}
    try:
        parts = urlparse(url)
    except ValueError:
        return "<unparsable-url>"
    if not parts.query:
        return url
    # keep_blank_values 保留 ``a=`` 这种空值参数，避免打码后丢字段。
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    redacted = [(k, "<redacted>" if k.lower() in keys else v) for k, v in pairs]
    return urlunparse(parts._replace(query=urlencode(redacted, safe="<>{}")))


def bookmark_url(record: dict) -> str | None:
    """Return the stored public destination for a non-article bookmark."""
    if record['type'] == 'page':
        return None
    for url in (record.get('external_url'), record.get('detail_url')):
        if not url or '\\' in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
            continue
        try:
            parts = urlparse(url)
            if (parts.scheme in {'http', 'https'} and parts.hostname and parts.username is None
                    and not any(key.lower() in SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parts.query))):
                return url
        except ValueError:
            continue
    return None


def redact_cookie(cookie_header: str) -> str:
    """把 Cookie header 压缩成 ``name=<redacted>`` 形式，只暴露 cookie 名。"""
    if not cookie_header:
        return ""
    names = []
    for chunk in cookie_header.split(";"):
        name, _, _ = chunk.strip().partition("=")
        if name:
            names.append(f"{name}=<redacted>")
    return "; ".join(names)


def sanitize_filename(name: str, *, max_length: int = 80, fallback: str = "untitled") -> str:
    """把任意字符串转成跨平台安全的单个路径片段。

    不保证唯一 —— 唯一性一律由 key 承担，这里只负责"可读且合法"。
    """
    if not name:
        return fallback
    # NFC 归一化，避免 macOS 分解形式与 Linux 组合形式在同一归档里分裂成两个目录。
    name = unicodedata.normalize("NFC", str(name))
    name = _ILLEGAL_FILENAME_CHARS.sub("_", name)
    name = _WHITESPACE_RUN.sub(" ", name).strip().strip(".")
    if not name:
        return fallback
    # 按字符截断后再确认 UTF-8 字节数不超标（中文 3 字节，容易撑爆 255 字节上限）。
    name = name[:max_length]
    while len(name.encode("utf-8")) > 200 and name:
        name = name[:-1]
    if not name:
        return fallback
    if name.upper().split(".")[0] in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name


def safe_key_dirname(key: str) -> str:
    """key 作为目录名。key 形如 ``0000000000000002-1000000002``，基本安全。

    仍然做一次净化以防服务端返回异常 key；若净化后为空则退化为 key 的 sha1 前 16 位。
    """
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("_", (key or "").strip())
    # 折叠所有 .. 序列：单个路径片段里的 .. 不构成穿越，但归档目录名不值得留这种歧义
    cleaned = re.sub(r"\.{2,}", "_", cleaned)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        return "key_" + hashlib.sha1((key or "").encode("utf-8")).hexdigest()[:16]
    return cleaned[:120]


def readable_dirname(date: str | None, title: str | None, key: str) -> str:
    """``<date>_<title>_<shortkey>`` 形式的可读名，用于静态站点等展示场景。"""
    short = safe_key_dirname(key)[:12]
    parts = []
    if date:
        parts.append(sanitize_filename(date, max_length=10, fallback="nodate"))
    if title:
        parts.append(sanitize_filename(title, max_length=40, fallback="untitled"))
    parts.append(short)
    return "_".join(p for p in parts if p)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str], chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Content-Type 到扩展名的显式映射。优先于 mimetypes，因为后者在不同系统上结果不一致
# （例如 image/jpeg 可能返回 .jpe），而归档文件名需要稳定可复现。
_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-flv": ".flv",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "application/x-shockwave-flash": ".swf",
    "text/html": ".html",
    "application/pdf": ".pdf",
}


def extension_for(content_type: str | None, url: str | None = None) -> str:
    """根据 Content-Type（优先）或 URL 路径推断扩展名，始终返回带点的小写扩展名。"""
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in _CONTENT_TYPE_EXT:
            return _CONTENT_TYPE_EXT[base]
        guessed = mimetypes.guess_extension(base)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    if url:
        path = urlparse(url).path
        _, ext = os.path.splitext(path)
        ext = ext.lower()
        # 老网页里常见 ``.jpg?v=2`` 这类，splitext 已经处理掉 query；再挡一次超长/异常扩展名。
        if 1 < len(ext) <= 6 and re.fullmatch(r"\.[a-z0-9]+", ext):
            return ext
    return ".bin"


def url_scheme_kind(url: str) -> str:
    """区分可下载的 http(s) 与 data:/javascript:/about: 等非下载 URL。"""
    if not url:
        return "empty"
    stripped = url.strip()
    lowered = stripped.lower()
    for scheme in ("data:", "javascript:", "about:", "mailto:", "tel:", "blob:"):
        if lowered.startswith(scheme):
            return scheme.rstrip(":")
    if lowered.startswith("//"):
        return "protocol-relative"
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "http"
    if "://" in lowered:
        return "other-scheme"
    return "relative"


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def truncate(text: str, limit: int = 60) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
