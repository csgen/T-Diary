-- tokenDiary schema (PLAN.md §5).
--
-- Conventions:
--   * every table carries created_at / updated_at in UTC;
--     they are DIFFERENT from local_date / local_hour, which are UTC+8
--   * updated_at is maintained by trigger so no call site can forget it
--   * usage_event is keyed by message_id: one API call, one row

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_source (
  source_id     TEXT PRIMARY KEY,
  label         TEXT NOT NULL,
  root_path     TEXT NOT NULL,
  account_uuid  TEXT,
  account_email TEXT,
  org_name      TEXT,
  last_seen     TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS dim_project (
  project_id INTEGER PRIMARY KEY,
  source_id  TEXT NOT NULL REFERENCES dim_source(source_id),
  path       TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(source_id, path)
);

CREATE TABLE IF NOT EXISTS dim_session (
  session_ref INTEGER PRIMARY KEY,
  source_id   TEXT NOT NULL REFERENCES dim_source(source_id),
  session_id  TEXT NOT NULL,
  project_id  INTEGER REFERENCES dim_project(project_id),
  first_ts    TEXT,
  last_ts     TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(source_id, session_id)
);

CREATE TABLE IF NOT EXISTS dim_file (
  file_id    INTEGER PRIMARY KEY,
  source_id  TEXT NOT NULL REFERENCES dim_source(source_id),
  rel_path   TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(source_id, rel_path)
);

CREATE TABLE IF NOT EXISTS ingest_batch (
  batch_id      INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  mode          TEXT,
  sources_json  TEXT,
  rows_inserted INTEGER NOT NULL DEFAULT 0,
  rows_revised  INTEGER NOT NULL DEFAULT 0,
  notes         TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS price_rev (
  rev          INTEGER PRIMARY KEY,
  applied_at   TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  prices_json  TEXT NOT NULL,
  note         TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS usage_event (
  message_id     TEXT PRIMARY KEY,
  request_id     TEXT,

  source_id      TEXT NOT NULL REFERENCES dim_source(source_id),
  account_uuid   TEXT,
  session_ref    INTEGER REFERENCES dim_session(session_ref),
  project_id     INTEGER REFERENCES dim_project(project_id),
  file_id        INTEGER REFERENCES dim_file(file_id),
  git_branch     TEXT,
  entrypoint     TEXT,

  model          TEXT NOT NULL,
  effort         TEXT,
  service_tier   TEXT,
  speed          TEXT,
  is_sidechain   INTEGER NOT NULL DEFAULT 0,
  agent_id       TEXT,

  ts_utc         TEXT NOT NULL,
  local_date     TEXT NOT NULL,
  local_hour     INTEGER NOT NULL,
  iso_week       TEXT NOT NULL,
  month          TEXT NOT NULL,
  year           INTEGER NOT NULL,
  tz_offset_minutes INTEGER,

  input_tokens          INTEGER NOT NULL DEFAULT 0,
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  thinking_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
  web_search_requests   INTEGER NOT NULL DEFAULT 0,
  web_fetch_requests    INTEGER NOT NULL DEFAULT 0,

  cost_usd            REAL,
  cost_breakdown_json TEXT,
  price_rev           INTEGER REFERENCES price_rev(rev),

  batch_id       INTEGER REFERENCES ingest_batch(batch_id),
  last_batch_id  INTEGER REFERENCES ingest_batch(batch_id),
  revision_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS ix_ue_date       ON usage_event(local_date);
CREATE INDEX IF NOT EXISTS ix_ue_acct_date  ON usage_event(account_uuid, local_date);
CREATE INDEX IF NOT EXISTS ix_ue_model_date ON usage_event(model, local_date);
CREATE INDEX IF NOT EXISTS ix_ue_session    ON usage_event(session_ref);
CREATE INDEX IF NOT EXISTS ix_ue_updated    ON usage_event(updated_at);

CREATE TABLE IF NOT EXISTS scan_state (
  source_id     TEXT NOT NULL REFERENCES dim_source(source_id),
  file_path     TEXT NOT NULL,
  size          INTEGER NOT NULL,
  mtime         REAL    NOT NULL,
  byte_offset   INTEGER NOT NULL DEFAULT 0,
  head_sha256   TEXT,
  anchor_len    INTEGER,
  anchor_sha256 TEXT,
  anchor_uuid   TEXT,
  last_scan     TEXT,
  last_result   TEXT,
  reset_count   INTEGER NOT NULL DEFAULT 0,
  deleted_at    TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (source_id, file_path)
);

CREATE TABLE IF NOT EXISTS scan_watermark (
  source_id         TEXT PRIMARY KEY REFERENCES dim_source(source_id),
  last_scan_started TEXT,
  last_full_scan    TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TRIGGER IF NOT EXISTS trg_dim_source_updated AFTER UPDATE ON dim_source FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE dim_source SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS trg_dim_project_updated AFTER UPDATE ON dim_project FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE dim_project SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS trg_dim_session_updated AFTER UPDATE ON dim_session FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE dim_session SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS trg_dim_file_updated AFTER UPDATE ON dim_file FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE dim_file SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS trg_ingest_batch_updated AFTER UPDATE ON ingest_batch FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE ingest_batch SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS trg_usage_event_updated AFTER UPDATE ON usage_event FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE usage_event SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS trg_scan_state_updated AFTER UPDATE ON scan_state FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE scan_state SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS trg_scan_watermark_updated AFTER UPDATE ON scan_watermark FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE scan_watermark SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS trg_price_rev_updated AFTER UPDATE ON price_rev FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE price_rev SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE rowid = NEW.rowid;
END;
