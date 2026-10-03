"""文件落盘。全部写操作都是原子的，任何时刻断电都不会留下半个 JSON。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .utils import decode_response_bytes, sha256_bytes


class IntegrityError(RuntimeError):
    """已保存数据或版本引用与记录不一致，不能静默覆盖或回退。"""


def write_atomic(path: str | os.PathLike[str], data: bytes, *, overwrite: bool = True) -> str:
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
        if overwrite:
            os.replace(tmp_name, target)
        else:
            # 同目录硬链接原子发布且不覆盖已有文件，消除 exists()/write() 竞争。
            os.link(tmp_name, target)
            os.unlink(tmp_name)
        if os.name == "posix":
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        # 包括 KeyboardInterrupt：绝不留下临时文件残骸。
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return sha256_bytes(data)


def write_once(path: str | os.PathLike[str], data: bytes) -> str:
    """相同内容复用，不同内容明确报错。"""
    try:
        return write_atomic(path, data, overwrite=False)
    except FileExistsError:
        if Path(path).read_bytes() != data:
            raise IntegrityError(f"不可覆盖已保存原件：{Path(path).name}") from None
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
    """保存不可变响应 + 旁路元数据，最后原子更新 latest 引用。

    首次仍使用旧路径；以后每次抓取另存到 <文件名>.versions/。
    已有正文与旁路元数据均不覆盖。latest 是派生索引，可变。
    """
    from .utils import redact_url, utcnow_iso

    original = Path(path)
    target = original
    try:
        digest = write_atomic(target, body, overwrite=False)
    except FileExistsError:
        target = original.parent / (original.name + ".versions") / (uuid4().hex + original.suffix)
        digest = write_once(target, body)
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
    write_once(str(target) + ".meta.json", (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode())
    write_json_atomic(str(original) + ".latest.json", {
        "version": 1, "path": str(target.relative_to(original.parent)), "sha256": digest,
    })
    return {**meta, "path": str(target)}


@dataclass
class RawSnapshot:
    path: Path
    content: bytes
    metadata: dict

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.content)

    @property
    def text(self) -> str:
        return decode_response_bytes(self.content, self.metadata.get("headers", {}))


def read_raw(path: str | os.PathLike[str]) -> RawSnapshot:
    """读取最新已完整发布的快照；无 latest 的旧归档直接兼容。"""
    original = Path(path)
    pointer_path = Path(str(original) + ".latest.json")
    target = original
    expected = None
    versions = original.parent / (original.name + ".versions")
    if not pointer_path.exists() and versions.exists() and any(versions.iterdir()):
        raise IntegrityError(f"版本引用缺失，不能自动确定当前版本：{original.name}")
    if pointer_path.exists():
        pointer = read_json(pointer_path)
        if not isinstance(pointer, dict) or pointer.get("version") != 1 or not isinstance(pointer.get("path"), str):
            raise IntegrityError(f"版本引用无效：{original.name}")
        relative = Path(pointer["path"])
        valid = relative == Path(original.name) or (
            len(relative.parts) == 2 and relative.parts[0] == original.name + ".versions"
            and relative.name not in {".", ".."}
        )
        if not valid or relative.is_absolute() or ".." in relative.parts:
            raise IntegrityError(f"版本路径越界：{original.name}")
        target = original.parent / relative
        if not target.resolve().is_relative_to(original.parent.resolve()):
            raise IntegrityError(f"版本路径越界：{original.name}")
        expected = pointer.get("sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            raise IntegrityError(f"版本摘要无效：{original.name}")
    try:
        body = target.read_bytes()
    except FileNotFoundError:
        if pointer_path.exists():
            raise IntegrityError(f"版本文件缺失：{original.name}") from None
        raise
    meta_path = Path(str(target) + ".meta.json")
    meta = read_json(meta_path, {})
    if not isinstance(meta, dict) or (meta_path.exists() and not meta.get("sha256")):
        raise IntegrityError(f"快照元数据无效：{target.name}")
    digest = sha256_bytes(body)
    if (expected and expected != digest) or (meta.get("sha256") and meta["sha256"] != digest):
        raise IntegrityError(f"快照校验失败：{target.name}")
    if pointer_path.exists() and not meta_path.exists():
        raise IntegrityError(f"快照元数据缺失：{target.name}")
    return RawSnapshot(target, body, meta)


def current_content_path(item_dir: Path, *, prefer_local: bool = True) -> Path:
    """导出和资源阶段共用的正文选择，避免重解析后继续使用旧的本地化文件。"""
    metadata = read_json(item_dir / "metadata.json", {}) or {}
    if metadata.get("has_content_container") is False:
        return item_dir / "content.unavailable.html"
    digest = metadata.get("parsed_sha256")
    local = item_dir / "content.local.html"
    if (prefer_local and local.exists() and (not digest or metadata.get("localized_from_sha256") == digest)
            and (not metadata.get("resource_fingerprint") or
                 metadata.get("localized_resource_fingerprint") == metadata["resource_fingerprint"])):
        if metadata.get("localized_sha256") and sha256_bytes(local.read_bytes()) != metadata["localized_sha256"]:
            raise IntegrityError("本地化正文校验失败，请重新运行 assets")
        return local
    if digest:
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise IntegrityError("解析正文摘要无效")
        parsed = item_dir / "parsed" / f"{digest}.html"
        if not parsed.exists() or sha256_bytes(parsed.read_bytes()) != digest:
            raise IntegrityError("解析正文版本缺失或校验失败")
        return parsed
    return item_dir / "content.original.html"
