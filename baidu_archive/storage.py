"""文件落盘。全部写操作都是原子的，任何时刻断电都不会留下半个 JSON。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .utils import sha256_bytes


def write_atomic(path: str | os.PathLike[str], data: bytes) -> str:
    """原子写入：同目录临时文件 → fsync → os.replace。返回内容 sha256。

    同目录是必须的 —— 跨文件系统 ``os.replace`` 不是原子操作。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        # 包括 KeyboardInterrupt：绝不留下临时文件残骸。
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return sha256_bytes(data)


def write_text_atomic(path: str | os.PathLike[str], text: str) -> str:
    return write_atomic(path, text.encode("utf-8"))


def write_json_atomic(path: str | os.PathLike[str], payload: Any) -> str:
    """JSON 一律 UTF-8 + ensure_ascii=False，保证中文在归档里可直接阅读。"""
    blob = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False, default=str)
    return write_text_atomic(path, blob + "\n")


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_text(path: str | os.PathLike[str], default: str | None = None) -> str | None:
    """读取本地归档文本。历史网页可能含非法字节，故用 errors='replace' 而非抛错。"""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def save_raw(
    path: str | os.PathLike[str],
    body: bytes,
    *,
    url: str,
    status_code: int,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保存原始响应 + 旁路 ``.meta.json``。

    meta 中的 URL 已打码；完整 URL 只存在于 item metadata 中。
    """
    from .utils import redact_url, utcnow_iso

    digest = write_atomic(path, body)
    interesting = {}
    for name in ("content-type", "content-length", "last-modified", "etag", "date", "content-encoding"):
        value = (headers or {}).get(name) or (headers or {}).get(name.title())
        if value:
            interesting[name] = value
    meta = {
        "url_redacted": redact_url(url),
        "http_status": status_code,
        "fetched_at": utcnow_iso(),
        "bytes": len(body),
        "sha256": digest,
        "headers": interesting,
    }
    if extra:
        meta.update(extra)
    write_json_atomic(str(path) + ".meta.json", meta)
    return meta
