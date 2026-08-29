"""SQLite storage: schema, dimensions, and the monotone upsert.

The invariant this module exists to protect: a day's totals never decrease, and
the result does not depend on when or how often a scan runs. Two rules enforce
it, and both are load-bearing.

  * `message_id` is the primary key, so re-reading a file is a no-op.
  * Token counts update ONLY when the new value is strictly greater, because a
    scan landing mid-stream sees partial counts.

`INSERT OR REPLACE` is forbidden anywhere in this file -- it would silently
overwrite a complete row with a partial one.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

SCHEMA_VERSION = 3

# Used by the handful of statements that stamp a time from SQL rather than
# relying on a column default. Same format as schema.sql: UTC, milliseconds.
NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def load_schema() -> str:
    """The DDL, read from schema.sql rather than assembled from f-strings."""
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return fh.read()


# Columns written on insert. Named parameters, not positional `?`, so this
# list is the single source of truth: adding a column here cannot silently
# shift every value after it into the wrong field.
INSERT_COLUMNS = (
    "message_id", "request_id", "source_id", "account_uuid", "session_ref",
    "project_id", "file_id", "git_branch", "entrypoint", "model", "effort",
    "service_tier", "speed", "is_sidechain", "agent_id", "ts_utc", "local_date",
    "local_hour", "iso_week", "month", "year", "tz_offset_minutes",
    "input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens",
    "cache_write_5m_tokens", "cache_write_1h_tokens", "web_search_requests",
    "web_fetch_requests", "batch_id", "last_batch_id",
)

# Token columns rise to the larger of stored and incoming -- never fall.
MONOTONE_COLUMNS = (
    "input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens",
    "cache_write_5m_tokens", "cache_write_1h_tokens",
    "web_search_requests", "web_fetch_requests",
)

# Monotone upsert (PLAN.md D3, §6.4). The WHERE clause is what makes a no-op
# rescan write nothing at all -- so `updated_at` moving means a row genuinely
# changed, not merely that it was re-read.
_SET_MONOTONE = ",\n  ".join(
    f"{c} = MAX(usage_event.{c}, excluded.{c})" for c in MONOTONE_COLUMNS
)

UPSERT = f"""
INSERT INTO usage_event ({", ".join(INSERT_COLUMNS)})
VALUES ({", ".join(":" + c for c in INSERT_COLUMNS)})
ON CONFLICT(message_id) DO UPDATE SET
  {_SET_MONOTONE},
  last_batch_id  = excluded.last_batch_id,
  revision_count = usage_event.revision_count + 1
WHERE excluded.output_tokens > usage_event.output_tokens
"""


@dataclass(slots=True)
class UpsertResult:
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0

    @property
    def seen(self) -> int:
        return self.inserted + self.revised + self.unchanged


# Declarative migration list: (version, table, column, declaration, reason).
# Both migrate() and pending_migrations() read it, so what gets applied and what
# gets reported can never drift apart. Column additions only.
COLUMN_MIGRATIONS = (
    (2, "usage_event", "speed", "TEXT",
     "fast mode prices Opus 5/4.8 at 2x standard; without it those rows under-cost"),
    (3, "usage_event", "tz_offset_minutes", "INTEGER",
     "records which offset produced this row's local_date, so a date is self-explaining"),
)


class SchemaTooNew(Exception):
    """The database was written by a newer build than this one."""


class Store:
    """Owns the database connection and every write to it."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.con = sqlite3.connect(db_path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.execute("PRAGMA journal_mode = WAL")
        self.con.execute("PRAGMA synchronous = NORMAL")
        self._projects: dict[tuple[str, str], int] = {}
        self._sessions: dict[tuple[str, str], int] = {}
        self._files: dict[tuple[str, str], int] = {}

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()

    # -- schema ------------------------------------------------------------

    def stored_version(self) -> int:
        """Schema version recorded in the database, or 0 if it predates `meta`."""
        try:
            row = self.con.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            return 0                       # meta table does not exist yet
        return int(row["value"]) if row else 0

    def _ensure_column(self, table: str, column: str, decl: str) -> bool:
        """Add a column if absent. Idempotent, so a fresh database created from
        schema.sql and an older one being migrated converge on the same shape."""
        cols = {r[1] for r in self.con.execute(f"PRAGMA table_info({table})")}
        if column in cols:
            return False
        self.con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True

    def pending_migrations(self) -> list[str]:
        """What migrate() would do, without doing it.

        Read-only, so a migration on a database holding irreplaceable history
        can be inspected before it is applied.
        """
        pending = []
        for version, table, column, _decl, reason in COLUMN_MIGRATIONS:
            cols = {r[1] for r in self.con.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                pending.append(f"v{version}: add {table}.{column} -- {reason}")
        return pending

    def migrate(self) -> list[str]:
        """Bring an existing database up to SCHEMA_VERSION. Additive only --
        no migration may drop or rewrite a column that holds usage data."""
        applied = []
        for version, table, column, decl, reason in COLUMN_MIGRATIONS:
            if self._ensure_column(table, column, decl):
                applied.append(f"v{version}: add {table}.{column} -- {reason}")
        self.con.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.con.commit()
        return applied

    def init_schema(self) -> list[str]:
        """Create or migrate the schema. Returns the migrations applied, if any.

        The return value is not decoration: a schema change to the database
        holding the only surviving copy of pruned history should never happen
        silently. Callers are expected to report it.
        """
        # The one job only a version number can do: refuse a database written by
        # newer code. Migrations are additive, so older code would not crash --
        # it would quietly ignore columns it does not know and write rows the
        # newer schema considers incomplete. Failing loudly is the whole point.
        found = self.stored_version()
        if found > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"{self.db_path} was written by a newer tokenDiary "
                f"(schema v{found}); this build understands v{SCHEMA_VERSION}. "
                f"Update the code, or point --config at a different data directory."
            )
        self.con.executescript(load_schema())
        self.con.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (str(SCHEMA_VERSION),),
        )
        self.con.commit()
        return self.migrate()

    def schema_version(self) -> int:
        row = self.con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 0

    # -- dimensions --------------------------------------------------------

    def upsert_source(self, source, account) -> None:
        """Record a source and the account currently signed in on it.

        Identity comes from the install's .claude.json, never from config, so
        re-logging into a different account is visible rather than mislabeled.
        """
        self.con.execute(
            f"""INSERT INTO dim_source
                  (source_id, label, root_path, account_uuid, account_email,
                   org_name, last_seen)
                VALUES (?,?,?,?,?,?,{NOW_SQL})
                ON CONFLICT(source_id) DO UPDATE SET
                  label = excluded.label,
                  root_path = excluded.root_path,
                  account_uuid = COALESCE(excluded.account_uuid, dim_source.account_uuid),
                  account_email = COALESCE(excluded.account_email, dim_source.account_email),
                  org_name = COALESCE(excluded.org_name, dim_source.org_name),
                  last_seen = {NOW_SQL}""",
            (source.id, source.label, source.path,
             account.uuid, account.email, account.org_name),
        )
        # The account cache is now stale; rebuild it on next use.
        if hasattr(self, "_acct_cache"):
            del self._acct_cache

    def _get_or_create(self, cache, key, select_sql, insert_sql, insert_args) -> int:
        if key in cache:
            return cache[key]
        row = self.con.execute(select_sql, key).fetchone()
        if row is None:
            cur = self.con.execute(insert_sql, insert_args)
            ident = cur.lastrowid
        else:
            ident = row[0]
        cache[key] = ident
        return ident

    def project_id(self, source_id: str, path: str | None) -> int | None:
        if not path:
            return None
        return self._get_or_create(
            self._projects, (source_id, path),
            "SELECT project_id FROM dim_project WHERE source_id=? AND path=?",
            "INSERT INTO dim_project(source_id, path) VALUES (?,?)",
            (source_id, path),
        )

    def file_id(self, source_id: str, rel_path: str) -> int:
        return self._get_or_create(
            self._files, (source_id, rel_path),
            "SELECT file_id FROM dim_file WHERE source_id=? AND rel_path=?",
            "INSERT INTO dim_file(source_id, rel_path) VALUES (?,?)",
            (source_id, rel_path),
        )

    def session_ref(self, source_id: str, session_id: str | None,
                    project_id: int | None) -> int | None:
        if not session_id:
            return None
        return self._get_or_create(
            self._sessions, (source_id, session_id),
            "SELECT session_ref FROM dim_session WHERE source_id=? AND session_id=?",
            "INSERT INTO dim_session(source_id, session_id, project_id) VALUES (?,?,?)",
            (source_id, session_id, project_id),
        )

    def touch_session(self, session_ref: int | None, ts: str) -> None:
        """Widen a session's time span. No-op when the span already covers ts,
        so updated_at does not churn on every ingest."""
        if session_ref is None:
            return
        self.con.execute(
            """UPDATE dim_session
                 SET first_ts = MIN(COALESCE(first_ts, :ts), :ts),
                     last_ts  = MAX(COALESCE(last_ts,  :ts), :ts)
               WHERE session_ref = :ref
                 AND (first_ts IS NULL OR :ts < first_ts
                   OR last_ts  IS NULL OR :ts > last_ts)""",
            {"ts": ts, "ref": session_ref},
        )

    # -- batches -----------------------------------------------------------

    def begin_batch(self, mode: str, sources_json: str) -> int:
        cur = self.con.execute(
            f"INSERT INTO ingest_batch(started_at, mode, sources_json) "
            f"VALUES ({NOW_SQL}, ?, ?)",
            (mode, sources_json),
        )
        self.con.commit()
        return cur.lastrowid

    def finish_batch(self, batch_id: int, result: UpsertResult, notes: str = "") -> None:
        self.con.execute(
            f"""UPDATE ingest_batch
                   SET finished_at = {NOW_SQL}, rows_inserted = ?,
                       rows_revised = ?, notes = ?
                 WHERE batch_id = ?""",
            (result.inserted, result.revised, notes or None, batch_id),
        )
        self.con.commit()

    # -- facts -------------------------------------------------------------

    def existing_ids(self, message_ids) -> set[str]:
        """Which of these message_ids are already stored.

        Needed to tell an insert from a revision: SQLite reports both as one
        changed row, and conflating them would make the M1 acceptance check
        ("second run inserts nothing") unverifiable.
        """
        found: set[str] = set()
        ids = list(message_ids)
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            placeholders = ",".join("?" * len(chunk))
            rows = self.con.execute(
                f"SELECT message_id FROM usage_event WHERE message_id IN ({placeholders})",
                chunk,
            )
            found.update(r[0] for r in rows)
        return found

    def upsert_events(self, records, batch_id: int) -> UpsertResult:
        """Apply the monotone upsert to a batch of parsed records."""
        result = UpsertResult()
        records = list(records)
        existing = self.existing_ids(r.message_id for r in records)

        for rec in records:
            project_id = self.project_id(rec.source_id, rec.project_path)
            session_ref = self.session_ref(rec.source_id, rec.session_id, project_id)
            file_id = self.file_id(rec.source_id, rec.rel_path)
            account_uuid = self._source_account.get(rec.source_id)

            # Most fields come straight off the record by name; only the four
            # resolved here differ. Order is irrelevant -- the SQL binds by
            # name, so a new column cannot shift the others out of place.
            params = {c: getattr(rec, c, None) for c in INSERT_COLUMNS}
            params.update(
                account_uuid=account_uuid, session_ref=session_ref,
                project_id=project_id, file_id=file_id,
                batch_id=batch_id, last_batch_id=batch_id,
            )
            cur = self.con.execute(UPSERT, params)
            if rec.message_id not in existing:
                result.inserted += 1
                self.touch_session(session_ref, rec.ts_utc)
            elif cur.rowcount:
                result.revised += 1
                self.touch_session(session_ref, rec.ts_utc)
            else:
                result.unchanged += 1
        return result

    @property
    def _source_account(self) -> dict[str, str | None]:
        if not hasattr(self, "_acct_cache"):
            self._acct_cache = {
                r["source_id"]: r["account_uuid"]
                for r in self.con.execute("SELECT source_id, account_uuid FROM dim_source")
            }
        return self._acct_cache

    # -- pricing -----------------------------------------------------------

    def check_price_rev(self, prices) -> tuple[int, bool]:
        """Validate the declared revision against what is stored.

        Returns (rev, is_new). Read-only -- registration is a separate step, so
        `--dry-run` can validate without writing.

        The revision number is whatever `prices.json` declares. The only thing
        enforced is that a given number always means the same rates: if rev 3
        is already stored and the file's rates differ from that snapshot, the
        costs recorded under rev 3 would no longer be explained by the snapshot
        behind them.
        """
        from .price import PriceRevConflict, semantic_hash

        row = self.con.execute(
            "SELECT rev, prices_json FROM price_rev WHERE rev=?", (prices.rev,)
        ).fetchone()
        if row is None:
            return prices.rev, True

        try:
            stored_hash = semantic_hash(json.loads(row["prices_json"]))
        except ValueError:
            stored_hash = None
        if stored_hash is not None and stored_hash != prices.content_hash:
            raise PriceRevConflict(
                f'{prices.path}: rev {prices.rev} is already recorded with different '
                f"rates. Bump \"rev\" to {self.max_price_rev() + 1} so the change gets "
                f"its own revision, then run `recost --dry-run` to see what it would "
                f"do to stored costs."
            )
        return prices.rev, False

    def max_price_rev(self) -> int:
        row = self.con.execute("SELECT COALESCE(MAX(rev), 0) m FROM price_rev").fetchone()
        return row["m"]

    def register_price_rev(self, prices) -> tuple[int, bool]:
        """Validate, then store the declared revision if it is not yet known."""
        rev, is_new = self.check_price_rev(prices)
        if is_new:
            self.con.execute(
                f"INSERT INTO price_rev(rev, applied_at, content_hash, prices_json, note) "
                f"VALUES (?, {NOW_SQL}, ?, ?, ?)",
                (rev, prices.content_hash, prices.raw, f"declared in {prices.path}"),
            )
            self.con.commit()
        return rev, is_new

    def set_cost(self, message_id: str, cost: float | None,
                 breakdown: str | None, rev: int | None) -> None:
        self.con.execute(
            "UPDATE usage_event SET cost_usd=?, cost_breakdown_json=?, price_rev=? "
            "WHERE message_id=?",
            (cost, breakdown, rev, message_id),
        )

    def uncosted(self):
        """Rows that have never been costed.

        Deliberately NOT "rows whose price_rev differs from the current one":
        ingest must price new rows and touch nothing else. Re-pricing already
        costed rows is `recost`'s job alone, so that editing prices.json can
        never quietly rewrite history through a routine scan (PLAN.md D6).
        """
        return self.con.execute(
            "SELECT * FROM usage_event WHERE price_rev IS NULL").fetchall()

    def rows_on_other_revs(self, rev: int) -> list[dict]:
        """Costed rows priced under some revision other than `rev`.

        Not an error -- a stored cost is meant to be stable. Surfaced so the
        drift is visible and `recost` is a deliberate choice.
        """
        return [dict(r) for r in self.con.execute(
            "SELECT price_rev, COUNT(*) n FROM usage_event "
            "WHERE price_rev IS NOT NULL AND price_rev != ? GROUP BY price_rev",
            (rev,))]

    def priced_between(self, since: str | None = None, model: str | None = None):
        sql = "SELECT * FROM usage_event WHERE 1=1"
        params: dict = {}
        if since:
            sql += " AND local_date >= :since"
            params["since"] = since
        if model:
            sql += " AND model = :model"
            params["model"] = model
        return self.con.execute(sql, params).fetchall()

    def cost_by_month(self, since: str | None = None) -> list[dict]:
        sql = ("SELECT month, COUNT(*) n, SUM(cost_usd) cost, "
               "SUM(cost_usd IS NULL) unpriced FROM usage_event")
        params: tuple = ()
        if since:
            sql += " WHERE local_date >= ?"
            params = (since,)
        sql += " GROUP BY month ORDER BY month"
        return [dict(r) for r in self.con.execute(sql, params)]

    def unknown_models(self) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT model, COUNT(*) n FROM usage_event WHERE cost_usd IS NULL "
            "GROUP BY model ORDER BY n DESC")]

    # -- derived dates -----------------------------------------------------

    def all_ts(self) -> list[dict]:
        """Every row's immutable ts_utc plus its currently derived date fields.

        `rebuild-dates` re-derives from ts_utc alone, so this is the only input
        it needs from the database.
        """
        return [dict(r) for r in self.con.execute(
            "SELECT message_id, ts_utc, local_date, local_hour, iso_week, month, "
            "year, tz_offset_minutes FROM usage_event ORDER BY ts_utc")]

    def set_dates(self, message_id: str, fields: dict) -> None:
        self.con.execute(
            "UPDATE usage_event SET local_date=:local_date, local_hour=:local_hour, "
            "iso_week=:iso_week, month=:month, year=:year, "
            "tz_offset_minutes=:tz_offset_minutes WHERE message_id=:message_id",
            {**fields, "message_id": message_id},
        )

    # -- scan state --------------------------------------------------------

    def load_scan_state(self, source_id: str) -> dict[str, dict]:
        return {
            r["file_path"]: dict(r)
            for r in self.con.execute(
                "SELECT * FROM scan_state WHERE source_id=? AND deleted_at IS NULL",
                (source_id,),
            )
        }

    def record_scan(self, source_id: str, file_path: str, size: int, mtime: float,
                    byte_offset: int, anchor_len: int, anchor_sha256: str | None,
                    anchor_uuid: str | None, result: str,
                    head_sha256: str | None = None, was_reset: bool = False) -> None:
        """Store where reading stopped and how to verify that point next time.

        `was_reset` counts fast paths that failed verification -- a file whose
        reset_count keeps climbing is churning, which is the visible symptom of
        a writer rewriting content we thought was settled.
        """
        self.con.execute(
            f"""INSERT INTO scan_state
                  (source_id, file_path, size, mtime, byte_offset, anchor_len,
                   anchor_sha256, anchor_uuid, head_sha256, last_scan,
                   last_result, reset_count)
                VALUES (?,?,?,?,?,?,?,?,?,{NOW_SQL},?,?)
                ON CONFLICT(source_id, file_path) DO UPDATE SET
                  size = excluded.size,
                  mtime = excluded.mtime,
                  byte_offset = excluded.byte_offset,
                  anchor_len = excluded.anchor_len,
                  anchor_sha256 = excluded.anchor_sha256,
                  anchor_uuid = excluded.anchor_uuid,
                  head_sha256 = COALESCE(excluded.head_sha256, scan_state.head_sha256),
                  last_scan = {NOW_SQL},
                  last_result = excluded.last_result,
                  reset_count = scan_state.reset_count + excluded.reset_count,
                  deleted_at = NULL""",
            (source_id, file_path, size, mtime, byte_offset, anchor_len,
             anchor_sha256, anchor_uuid, head_sha256, result, 1 if was_reset else 0),
        )

    def mark_deleted(self, source_id: str, paths) -> int:
        """Stamp files Claude has pruned. Rows are never removed (PLAN.md D2)."""
        n = 0
        for path in paths:
            self.con.execute(
                f"""UPDATE scan_state SET deleted_at = {NOW_SQL}, last_result = 'deleted'
                      WHERE source_id=? AND file_path=? AND deleted_at IS NULL""",
                (source_id, path),
            )
            n += 1
        return n

    def watermark(self, source_id: str) -> float | None:
        row = self.con.execute(
            "SELECT last_scan_started FROM scan_watermark WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if not row or not row["last_scan_started"]:
            return None
        import datetime
        return datetime.datetime.fromisoformat(
            row["last_scan_started"].replace("Z", "+00:00")
        ).timestamp()

    def set_watermark(self, source_id: str, started_at: str, full: bool = False) -> None:
        """Advance a source's watermark. Called only on success, per source, so
        a failed run does not skip a day of that source's files."""
        self.con.execute(
            """INSERT INTO scan_watermark(source_id, last_scan_started, last_full_scan)
               VALUES (?,?,?)
               ON CONFLICT(source_id) DO UPDATE SET
                 last_scan_started = excluded.last_scan_started,
                 last_full_scan = COALESCE(excluded.last_full_scan,
                                           scan_watermark.last_full_scan)""",
            (source_id, started_at, started_at if full else None),
        )

    # -- reporting ---------------------------------------------------------

    def stats(self) -> dict:
        q = self.con.execute
        row = q("""SELECT COUNT(*) n, MIN(local_date) d0, MAX(local_date) d1,
                          COUNT(DISTINCT local_date) days,
                          SUM(input_tokens + output_tokens + cache_read_tokens
                              + cache_write_5m_tokens + cache_write_1h_tokens) tokens,
                          SUM(revision_count > 0) revised
                     FROM usage_event""").fetchone()
        return {
            "rows": row["n"], "first_date": row["d0"], "last_date": row["d1"],
            "active_days": row["days"], "tokens": row["tokens"] or 0,
            "revised_rows": row["revised"] or 0,
            "db_bytes": os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0,
            "by_source": [dict(r) for r in q(
                """SELECT s.source_id, s.label, s.account_email,
                          COUNT(e.message_id) rows,
                          MIN(e.local_date) d0, MAX(e.local_date) d1
                     FROM dim_source s LEFT JOIN usage_event e USING(source_id)
                    GROUP BY s.source_id ORDER BY s.source_id""")],
            "pruned_files": q(
                "SELECT COUNT(*) n FROM scan_state WHERE deleted_at IS NOT NULL"
            ).fetchone()["n"],
            "batches": q("SELECT COUNT(*) n FROM ingest_batch").fetchone()["n"],
        }
