-- Schema before resource identity and migration versioning.

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
