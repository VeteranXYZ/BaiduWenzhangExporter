"""Read archive resources from one descriptor anchored below the trusted root."""
from __future__ import annotations

import os
import stat
from pathlib import Path


SAFE_DESCRIPTOR_READ = (hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY') and
                        os.open in os.supports_dir_fd)


class UnsafeRead(OSError):
    pass


def read_resource(root: Path, relative: Path) -> tuple[bytes, os.stat_result]:
    if relative.is_absolute() or not relative.parts or any(p in {'.', '..'} for p in relative.parts):
        raise UnsafeRead('unsafe_relative_path')
    # No silent path-based fallback: platforms lacking these guarantees fail closed.
    if not SAFE_DESCRIPTOR_READ:
        raise UnsafeRead('safe_descriptor_read_unavailable')
    descriptors = []
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(parent)
        for part in relative.parts[:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(parent)
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeRead('not_regular_file')
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise UnsafeRead('file_changed_during_read')
        body = b''.join(chunks)
        if len(body) != before.st_size:
            raise UnsafeRead('file_changed_during_read')
        return body, before
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
