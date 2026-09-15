"""SQLite 状态存储。

选择 SQLite 而非 crawl_state.json 的原因是并发写入下的事务原子性 ——
2 个 worker 同时推进状态时，JSON 读改写会丢更新。
``master_index.json`` / ``crawl_state.json`` 仍作为派生产物导出，保证归档可离线检视。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .utils import utcnow_iso

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
    UNIQUE(key, original_url, attribute)
);
CREATE INDEX IF NOT EXISTS idx_assets_key    ON assets(key);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(download_status);

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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")  # 归档场景：宁可慢也不丢
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

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
                                    detail_url,external_url,list_offset,position_in_page,
                                    global_position,raw_html,li_attrs_json,first_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    type=excluded.type,
                    title=COALESCE(excluded.title, records.title),
                    list_date=COALESCE(excluded.list_date, records.list_date),
                    list_date_text=COALESCE(excluded.list_date_text, records.list_date_text),
                    tags_json=excluded.tags_json,
                    list_desc=COALESCE(excluded.list_desc, records.list_desc),
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
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO assets(key,kind,element_type,attribute,original_url,resolved_url,
                                   scheme_kind,domain,downloadable)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key,original_url,attribute) DO UPDATE SET
                    kind=excluded.kind, resolved_url=excluded.resolved_url,
                    scheme_kind=excluded.scheme_kind, domain=excluded.domain,
                    downloadable=excluded.downloadable
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
                ),
            )

    def record_asset_result(self, key: str, original_url: str, attribute: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{name}=?" for name in fields)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE assets SET {columns} WHERE key=? AND original_url=? AND attribute=?",
                (*fields.values(), key, original_url, attribute),
            )

    def assets_for(self, key: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM assets WHERE key=? ORDER BY id", (key,)).fetchall()
        return [dict(r) for r in rows]

    def all_assets(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM assets ORDER BY key, id").fetchall()
        return [dict(r) for r in rows]

    def asset_counts(self) -> dict[str, int]:
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"])
            rows = self._conn.execute(
                "SELECT download_status, COUNT(*) AS n FROM assets WHERE download_attempted=1 GROUP BY download_status"
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
