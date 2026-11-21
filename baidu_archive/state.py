"""SQLite 状态存储。

选择 SQLite 而非 crawl_state.json 的原因是并发写入下的事务原子性 ——
2 个 worker 同时推进状态时，JSON 读改写会丢更新。
``master_index.json`` / ``crawl_state.json`` 仍作为派生产物导出，保证归档可离线检视。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .utils import utcnow_iso

SCHEMA_VERSION = 1


class SchemaVersionError(RuntimeError):
    """数据库由更新的程序创建，当前版本不能安全修改。"""


def database_version(path: Path) -> int:
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as database:
        return database.execute('PRAGMA user_version').fetchone()[0]


def backup_before_migration(path: Path) -> Path:
    """Verify a standalone SQLite snapshot before replacing any archive state."""
    folder = path.parent / 'backups'
    temporary = None
    try:
        folder.mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.state-', suffix='.partial', dir=folder)
        temporary = Path(name)
        os.close(fd)
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(temporary)) as snapshot:
                source.backup(snapshot)
                snapshot.execute('PRAGMA journal_mode=DELETE')
                if snapshot.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise sqlite3.DatabaseError('备份完整性检查失败')
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        target = folder / f'state-before-migration-{uuid4().hex}.db'
        temporary.rename(target)
        if os.name == 'posix':
            directory = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return target
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f'无法备份归档数据库，已停止迁移：{exc}') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- master index：504 条全部入库，page/article/other 一视同仁
CREATE TABLE IF NOT EXISTS records (
    key              TEXT PRIMARY KEY,
    type             TEXT NOT NULL,
    title            TEXT,
    list_date        TEXT,
    list_date_text   TEXT,
    tags_json        TEXT,
    list_desc        TEXT,
    list_source      TEXT,
    detail_url       TEXT,
    external_url     TEXT,
    list_offset      INTEGER,
    position_in_page INTEGER,
    global_position  INTEGER,
    raw_html         TEXT,
    li_attrs_json    TEXT,
    first_seen_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_type ON records(type);
CREATE INDEX IF NOT EXISTS idx_records_pos  ON records(global_position);

-- 抓取状态机（仅 page 类型进入）
CREATE TABLE IF NOT EXISTS items (
    key                TEXT PRIMARY KEY,
    status             TEXT NOT NULL DEFAULT 'pending',
    detail_fetched_at  TEXT,
    content_fetched_at TEXT,
    parsed_at          TEXT,
    assets_done_at     TEXT,
    iframe_url         TEXT,
    content_title      TEXT,
    content_date       TEXT,
    content_date_text  TEXT,
    source             TEXT,
    content_length     INTEGER,
    detail_sha256      TEXT,
    content_sha256     TEXT,
    warning            TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    first_attempt_at   TEXT,
    last_attempt_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);

CREATE TABLE IF NOT EXISTS assets (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    key                TEXT NOT NULL,
    kind               TEXT,
    element_type       TEXT,
    attribute          TEXT,
    original_url       TEXT NOT NULL,
    resolved_url       TEXT,
    scheme_kind        TEXT,
    domain             TEXT,
    downloadable       INTEGER DEFAULT 0,
    download_attempted INTEGER DEFAULT 0,
    download_status    TEXT,
    http_status        INTEGER,
    content_type       TEXT,
    local_path         TEXT,
    sha256             TEXT,
    size               INTEGER,
    error              TEXT,
    active             INTEGER NOT NULL DEFAULT 1,
    resource_identity  TEXT,
    download_identity  TEXT,
    UNIQUE(key, original_url, attribute)
);
CREATE INDEX IF NOT EXISTS idx_assets_key    ON assets(key);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(download_status);

CREATE TABLE IF NOT EXISTS asset_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    record_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT,
    stage        TEXT NOT NULL,
    url_redacted TEXT,
    http_status  INTEGER,
    error        TEXT,
    attempt      INTEGER,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_failures_key ON failures(key);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT,
    started_at  TEXT,
    finished_at TEXT,
    stats_json  TEXT
);
"""

# 状态推进是单调的：数字越大越靠后，永不回退。
STATUS_ORDER = {
    "pending": 0,
    "detail_fetched": 1,
    "content_fetched": 2,
    "parsed": 3,
    "assets_partial": 4,
    "complete": 5,
}
TERMINAL_OK = "complete"
SIDE_STATUSES = {"failed", "auth_error", "warning", "skipped_non_page"}


class ArchiveState:
    """状态存储。线程安全（WAL + 单连接 + 互斥锁）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self.schema_backup: Path | None = None
        try:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise SchemaVersionError("数据库版本较新，请使用创建它的程序版本")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            if version < SCHEMA_VERSION:
                self._initialize_schema()
            # Journal mode transition itself may return BUSY immediately, even
            # with busy_timeout. Retry only lock contention within the same bound.
            deadline = time.monotonic() + 30.0
            while True:
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        except BaseException:
            self._conn.rollback()
            self._conn.close()
            raise

    def _initialize_schema(self) -> None:
        # Reserve the write transaction before snapshotting through a separate read connection.
        self._conn.execute('BEGIN IMMEDIATE')
        version = self._conn.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SchemaVersionError('数据库版本较新，请升级程序')
        if version < SCHEMA_VERSION:
            if self._conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone():
                self.schema_backup = backup_before_migration(self.db_path)
            # executescript commits the transaction before DDL, so execute each statement here.
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    self._conn.execute(statement)
            self._migrate()
            self._conn.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
        self._conn.commit()

    # 归档工具不能因为加了一列就让已有 state.db 作废，故做幂等的补列迁移。
    _MIGRATIONS = (("records", "list_source", "TEXT"),
                   ("assets", "active", "INTEGER NOT NULL DEFAULT 1"),
                   ("assets", "resource_identity", "TEXT"),
                   ("assets", "download_identity", "TEXT"))

    def _migrate(self) -> None:
        for table, column, coltype in self._MIGRATIONS:
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "ArchiveState":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---------------- meta ----------------
    def set_meta(self, key: str, value: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    # ---------------- records ----------------
    def upsert_record(self, record: dict[str, Any]) -> None:
        """写入 master index。重复运行 index 时保留 first_seen_at 与原始顺序。"""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO records(key,type,title,list_date,list_date_text,tags_json,list_desc,
                                    list_source,detail_url,external_url,list_offset,position_in_page,
                                    global_position,raw_html,li_attrs_json,first_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    type=excluded.type,
                    title=COALESCE(excluded.title, records.title),
                    list_date=COALESCE(excluded.list_date, records.list_date),
                    list_date_text=COALESCE(excluded.list_date_text, records.list_date_text),
                    tags_json=excluded.tags_json,
                    list_desc=COALESCE(excluded.list_desc, records.list_desc),
                    list_source=COALESCE(excluded.list_source, records.list_source),
                    detail_url=COALESCE(excluded.detail_url, records.detail_url),
                    external_url=COALESCE(excluded.external_url, records.external_url),
                    list_offset=excluded.list_offset,
                    position_in_page=excluded.position_in_page,
                    global_position=excluded.global_position,
                    raw_html=excluded.raw_html,
                    li_attrs_json=excluded.li_attrs_json
                """,
                (
                    record["key"],
                    record.get("type") or "other",
                    record.get("title"),
                    record.get("list_date"),
                    record.get("list_date_text"),
                    json.dumps(record.get("tags") or [], ensure_ascii=False),
                    record.get("list_desc"),
                    record.get("list_source"),
                    record.get("detail_url"),
                    record.get("external_url"),
                    record.get("list_offset"),
                    record.get("position_in_page"),
                    record.get("global_position"),
                    record.get("raw_html"),
                    json.dumps(record.get("li_attrs") or {}, ensure_ascii=False),
                    utcnow_iso(),
                ),
            )

    def ensure_item(self, key: str, status: str = "pending") -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO items(key,status) VALUES(?,?)", (key, status))

    def get_record(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM records WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def iter_records(self, record_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM records"
        params: tuple = ()
        if record_type:
            sql += " WHERE type=?"
            params = (record_type,)
        sql += " ORDER BY global_position ASC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def count_records(self, record_type: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM records"
        params: tuple = ()
        if record_type:
            sql += " WHERE type=?"
            params = (record_type,)
        with self._lock:
            return int(self._conn.execute(sql, params).fetchone()["n"])

    # ---------------- items ----------------
    def get_item(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def update_item(self, key: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{name}=?" for name in fields)
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO items(key) VALUES(?)", (key,))
            conn.execute(f"UPDATE items SET {columns} WHERE key=?", (*fields.values(), key))

    def advance_status(self, key: str, status: str, **fields: Any) -> None:
        """只向前推进主线状态；旁路状态（failed/warning/…）直接覆盖。"""
        current = self.get_item(key)
        current_status = (current or {}).get("status", "pending")
        if status in STATUS_ORDER and current_status in STATUS_ORDER:
            if STATUS_ORDER[status] < STATUS_ORDER[current_status]:
                self.update_item(key, **fields)
                return
        self.update_item(key, status=status, **fields)

    def mark_attempt(self, key: str) -> None:
        now = utcnow_iso()
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO items(key) VALUES(?)", (key,))
            conn.execute(
                """UPDATE items SET attempts=attempts+1,
                       first_attempt_at=COALESCE(first_attempt_at,?),
                       last_attempt_at=? WHERE key=?""",
                (now, now, key),
            )

    def items_by_status(self, statuses: list[str]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT i.*, r.title AS list_title, r.type AS record_type, r.global_position
                    FROM items i JOIN records r ON r.key=i.key
                    WHERE i.status IN ({placeholders})
                    ORDER BY r.global_position ASC""",
                statuses,
            ).fetchall()
        return [dict(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def type_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT type, COUNT(*) AS n FROM records GROUP BY type").fetchall()
        return {r["type"]: int(r["n"]) for r in rows}

    # ---------------- assets ----------------
    def upsert_asset(self, key: str, asset: dict[str, Any]) -> None:
        from .truth import resource_identity
        identity = resource_identity({**asset, 'key': key})
        with self._tx() as conn:
            old = conn.execute('SELECT * FROM assets WHERE key=? AND original_url=? AND attribute=?',
                               (key, asset['original_url'], asset['attribute'])).fetchone()
            if old and resource_identity(dict(old)) != identity:
                self._remember_asset(conn, dict(old), 'resolved_url_changed')
                conn.execute('''UPDATE assets SET download_attempted=0, download_status=NULL,
                    http_status=NULL, content_type=NULL, local_path=NULL, sha256=NULL, size=NULL,
                    error=NULL, download_identity=NULL WHERE id=?''', (old['id'],))
            conn.execute(
                """
                INSERT INTO assets(key,kind,element_type,attribute,original_url,resolved_url,
                                   scheme_kind,domain,downloadable,resource_identity)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key,original_url,attribute) DO UPDATE SET
                    kind=excluded.kind, resolved_url=excluded.resolved_url,
                    scheme_kind=excluded.scheme_kind, domain=excluded.domain,
                    downloadable=excluded.downloadable, resource_identity=excluded.resource_identity
                """,
                (
                    key,
                    asset.get("kind"),
                    asset.get("element_type"),
                    asset.get("attribute"),
                    asset.get("original_url"),
                    asset.get("resolved_url"),
                    asset.get("scheme_kind"),
                    asset.get("domain"),
                    1 if asset.get("downloadable") else 0,
                    identity,
                ),
            )

    def record_asset_result(self, key: str, original_url: str, attribute: str, **fields: Any) -> None:
        if not fields:
            return
        with self._tx() as conn:
            row = conn.execute('SELECT * FROM assets WHERE key=? AND original_url=? AND attribute=?',
                               (key, original_url, attribute)).fetchone()
            if row:
                old = dict(row)
                if fields.get('download_status') == 'ok':
                    from .truth import resource_identity
                    fields.setdefault('download_identity', resource_identity(old))
                replacement = any(name in fields and fields[name] != old.get(name)
                                  for name in ('local_path', 'sha256', 'size', 'download_identity', 'download_status'))
                if old.get('download_status') == 'ok' and old.get('local_path') and replacement:
                    self._remember_asset(conn, old, 'download_replaced')
            columns = ", ".join(f"{name}=?" for name in fields)
            conn.execute(
                f"UPDATE assets SET {columns} WHERE key=? AND original_url=? AND attribute=?",
                (*fields.values(), key, original_url, attribute),
            )

    def _remember_asset(self, conn, asset: dict, reason: str) -> None:
        conn.execute('INSERT INTO asset_history(key,recorded_at,reason,record_json) VALUES(?,?,?,?)',
                     (asset['key'], utcnow_iso(), reason, json.dumps(asset, ensure_ascii=False)))

    def invalidate_asset(self, asset: dict, reason: str) -> None:
        """Retain the old path/hash as evidence; never delete the actual file."""
        with self._tx() as conn:
            self._remember_asset(conn, asset, reason)
            conn.execute('''UPDATE assets SET download_status='pending', download_identity=NULL,
                local_path=NULL, sha256=NULL, size=NULL, error=? WHERE id=?''', (reason, asset['id']))

    def asset_history(self, key: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute('SELECT * FROM asset_history WHERE key=? ORDER BY id', (key,)).fetchall()
        return [{**dict(r), 'record': json.loads(r['record_json'])} for r in rows]

    def set_current_assets(self, key: str, assets: list[dict[str, Any]]) -> None:
        """只切换当前正文的资源集合，历史资源记录与下载文件仍保留。"""
        with self._tx() as conn:
            conn.execute("UPDATE assets SET active=0 WHERE key=?", (key,))
            conn.executemany("UPDATE assets SET active=1 WHERE key=? AND original_url=? AND attribute=?",
                             [(key, a["original_url"], a["attribute"]) for a in assets])

    def assets_for(self, key: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM assets WHERE key=?" +
                                      ("" if include_inactive else " AND active=1") + " ORDER BY id", (key,)).fetchall()
        return [dict(r) for r in rows]

    def all_assets(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM assets WHERE active=1 ORDER BY key, id").fetchall()
        return [dict(r) for r in rows]

    def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        """按主键读取当前资源，避免缩略图请求逐次扫描全库。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM assets WHERE id=? AND active=1", (asset_id,)).fetchone()
        return dict(row) if row else None

    def backup_to(self, path: Path) -> None:
        """Write a consistent database snapshot, including any committed WAL pages."""
        with self._lock:
            destination = sqlite3.connect(str(path))
            try:
                self._conn.backup(destination)
            finally:
                destination.close()

    def asset_counts(self) -> dict[str, int]:
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) AS n FROM assets WHERE active=1").fetchone()["n"])
            rows = self._conn.execute(
                "SELECT download_status, COUNT(*) AS n FROM assets WHERE active=1 AND download_attempted=1 GROUP BY download_status"
            ).fetchall()
        counts = {"discovered": total}
        for row in rows:
            counts[row["download_status"] or "unknown"] = int(row["n"])
        return counts

    # ---------------- failures ----------------
    def record_failure(
        self,
        key: str | None,
        stage: str,
        *,
        url_redacted: str | None = None,
        http_status: int | None = None,
        error: str | None = None,
        attempt: int | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO failures(key,stage,url_redacted,http_status,error,attempt,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (key, stage, url_redacted, http_status, error, attempt, utcnow_iso()),
            )

    def clear_failures(self, key: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM failures WHERE key=?", (key,))

    def all_failures(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM failures ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    # ---------------- runs ----------------
    def start_run(self, command: str) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO runs(command,started_at) VALUES(?,?)", (command, utcnow_iso())
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, stats_json=? WHERE id=?",
                (utcnow_iso(), json.dumps(stats, ensure_ascii=False), run_id),
            )

    @contextmanager
    def tracked_run(self, command: str) -> Iterator[dict[str, Any]]:
        """保存正常完成、认证中止、手动停止与异常结果，不保存异常中的敏感文本。"""
        from .client import AuthExpired
        from .tasks import TaskStopped

        run_id = self.start_run(command)
        stats: dict[str, Any] = {}
        try:
            yield stats
            stats["outcome"] = ("auth_error" if stats.get("aborted") else
                                "partial" if stats.get("failed") or stats.get("warning") or stats.get("missing_raw") or stats.get("incomplete_items") else
                                "completed")
        except (KeyboardInterrupt, TaskStopped):
            stats["outcome"] = "stopped"
            raise
        except AuthExpired:
            stats["outcome"] = "auth_error"
            raise
        except BaseException as exc:
            stats.update(outcome="failed", error_type=type(exc).__name__)
            raise
        finally:
            if stats.get('outcome') not in {'completed', 'partial'}:
                stats.pop('download', None)
            self.finish_run(run_id, stats)

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
        results = []
        for row in rows:
            data = dict(row)
            data["stats"] = json.loads(data.pop("stats_json") or "{}")
            results.append(data)
        return results

    # ---------------- 一致性检查 ----------------
    def duplicate_titles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT title, COUNT(*) AS n, GROUP_CONCAT(key) AS keys FROM records
                   WHERE title IS NOT NULL AND title<>'' GROUP BY title HAVING n>1 ORDER BY n DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    def duplicate_content_hashes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT content_sha256 AS sha, COUNT(*) AS n, GROUP_CONCAT(key) AS keys FROM items
                   WHERE content_sha256 IS NOT NULL GROUP BY content_sha256 HAVING n>1 ORDER BY n DESC"""
            ).fetchall()
        return [dict(r) for r in rows]
