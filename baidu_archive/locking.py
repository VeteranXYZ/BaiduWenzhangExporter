"""按归档目录加进程锁，防止 CLI 与未来桌面任务同时修改同一批文件。"""

import os
from pathlib import Path


class ArchiveBusy(RuntimeError):
    pass


class ArchiveLock:
    def __init__(self, root: Path):
        self.path = Path(root).resolve() / ".archive.lock"
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self._handle.write(b"0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.close()
            self._handle = None
            raise ArchiveBusy("该归档目录已有任务正在运行，请等待当前任务结束或停止后重试") from None
        return self

    def __exit__(self, *exc):
        if self._handle:
            self._handle.close()  # OS releases the lock even if the process is terminated.
            self._handle = None
        # 不删除锁文件：删除后其他进程可能锁住不同 inode，失去互斥。
