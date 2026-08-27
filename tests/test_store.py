"""Tests for the M1 storage layer.

These pin the invariants that make the database trustworthy (PLAN.md §13):
a day's totals never decrease, re-reading a file changes nothing, and a
missing source file never removes a row.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.parse import UsageRecord  # noqa: E402
from src.store import SCHEMA_VERSION, Store  # noqa: E402


class FakeSource:
    def __init__(self, sid="host", label="Host", path="/root"):
        self.id, self.label, self.path = sid, label, path


class FakeAccount:
    def __init__(self, uuid="acct-1", email="a@example.com", org="Org"):
        self.uuid, self.email, self.org_name = uuid, email, org


def rec(mid="msg_a", out=100, source_id="host", **kw):
    base = dict(
        message_id=mid, request_id="req_1", source_id=source_id, session_id="sess-1",
        project_path="/home/dev/proj", rel_path="proj/sess.jsonl", git_branch="main",
        entrypoint="cli", model="claude-opus-5", effort="high", service_tier="standard",
        speed="standard",
        is_sidechain=0, agent_id=None, ts_utc="2026-06-29T08:00:00.000Z",
        local_date="2026-06-29", local_hour=16, iso_week="2026-W27",
        month="2026-06", year=2026, input_tokens=10, output_tokens=out,
        thinking_tokens=1, cache_read_tokens=500, cache_write_5m_tokens=20,
        cache_write_1h_tokens=40, web_search_requests=0, web_fetch_requests=0,
    )
    base.update(kw)
    return UsageRecord(**base)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.db"))
        self.store.init_schema()
        self.store.upsert_source(FakeSource(), FakeAccount())

    def tearDown(self):
        self.store.close()

    def ingest(self, records, batch_mode="incremental"):
        batch = self.store.begin_batch(batch_mode, "[]")
        result = self.store.upsert_events(records, batch)
        self.store.finish_batch(batch, result)
        self.store.con.commit()
        return result

    def row(self, mid="msg_a"):
        return self.store.con.execute(
            "SELECT * FROM usage_event WHERE message_id=?", (mid,)).fetchone()


class MonotoneUpsertTests(StoreTestCase):
    def test_first_ingest_inserts(self):
        r = self.ingest([rec(out=100)])
        self.assertEqual((r.inserted, r.revised, r.unchanged), (1, 0, 0))

    def test_reingesting_identical_data_changes_nothing(self):
        """The M1 acceptance check: a second run must be a complete no-op."""
        self.ingest([rec(out=100)])
        before = self.row()["updated_at"]
        r = self.ingest([rec(out=100)])
        self.assertEqual((r.inserted, r.revised, r.unchanged), (0, 0, 1))
        self.assertEqual(self.row()["updated_at"], before, "updated_at must not move")
        self.assertEqual(self.row()["revision_count"], 0)

    def test_larger_output_revises_upward(self):
        """A call caught mid-stream completes on a later scan."""
        self.ingest([rec(out=3)])
        r = self.ingest([rec(out=114)])
        self.assertEqual((r.inserted, r.revised, r.unchanged), (0, 1, 0))
        self.assertEqual(self.row()["output_tokens"], 114)
        self.assertEqual(self.row()["revision_count"], 1)

    def test_smaller_output_is_ignored(self):
        """A day's totals never decrease (PLAN.md §13 invariant 4)."""
        self.ingest([rec(out=114)])
        before = self.row()["updated_at"]
        r = self.ingest([rec(out=3)])
        self.assertEqual((r.inserted, r.revised, r.unchanged), (0, 0, 1))
        self.assertEqual(self.row()["output_tokens"], 114)
        self.assertEqual(self.row()["updated_at"], before)

    def test_revision_updates_provenance_columns(self):
        """A revision advances last_batch_id and revision_count, and leaves
        created_at and batch_id -- which record the insert -- alone.

        updated_at is only asserted non-decreasing: it has millisecond
        resolution, and two ingests inside one test run can land in the same
        millisecond. Real runs are seconds apart, so this is a test artifact,
        not a gap in the audit trail. revision_count is the exact signal.
        """
        self.ingest([rec(out=3)])
        first = self.row()
        self.ingest([rec(out=114)])
        second = self.row()
        self.assertGreaterEqual(second["updated_at"], first["updated_at"])
        self.assertEqual(second["created_at"], first["created_at"], "created_at is write-once")
        self.assertEqual(second["batch_id"], first["batch_id"], "batch_id records the insert")
        self.assertGreater(second["last_batch_id"], first["last_batch_id"])
        self.assertEqual(second["revision_count"], 1)

    def test_every_token_column_is_monotone(self):
        self.ingest([rec(out=100, input_tokens=50, cache_read_tokens=900,
                         thinking_tokens=7, cache_write_5m_tokens=11,
                         cache_write_1h_tokens=13, web_search_requests=2)])
        # Same output so no revision fires; smaller values must not land anyway.
        self.ingest([rec(out=200, input_tokens=1, cache_read_tokens=1,
                         thinking_tokens=1, cache_write_5m_tokens=1,
                         cache_write_1h_tokens=1, web_search_requests=1)])
        r = self.row()
        self.assertEqual(
            (r["input_tokens"], r["cache_read_tokens"], r["thinking_tokens"],
             r["cache_write_5m_tokens"], r["cache_write_1h_tokens"], r["web_search_requests"]),
            (50, 900, 7, 11, 13, 2),
        )

    def test_immutable_columns_survive_a_revision(self):
        self.ingest([rec(out=3)])
        self.ingest([rec(out=114, ts_utc="1999-01-01T00:00:00.000Z",
                         local_date="1999-01-01", model="claude-haiku-4-5-20251001")])
        r = self.row()
        self.assertEqual(r["ts_utc"], "2026-06-29T08:00:00.000Z")
        self.assertEqual(r["local_date"], "2026-06-29")
        self.assertEqual(r["model"], "claude-opus-5")

    def test_distinct_calls_coexist(self):
        r = self.ingest([rec("a", out=1), rec("b", out=2)])
        self.assertEqual(r.inserted, 2)


class DimensionTests(StoreTestCase):
    def test_dimensions_are_deduplicated(self):
        self.ingest([rec("a"), rec("b"), rec("c")])
        q = self.store.con.execute
        self.assertEqual(q("SELECT COUNT(*) FROM dim_project").fetchone()[0], 1)
        self.assertEqual(q("SELECT COUNT(*) FROM dim_session").fetchone()[0], 1)
        self.assertEqual(q("SELECT COUNT(*) FROM dim_file").fetchone()[0], 1)

    def test_session_span_widens_then_stops_churning(self):
        self.ingest([rec("a", ts_utc="2026-06-29T10:00:00.000Z")])
        self.ingest([rec("b", ts_utc="2026-06-29T08:00:00.000Z")])
        self.ingest([rec("c", ts_utc="2026-06-29T12:00:00.000Z")])
        row = self.store.con.execute("SELECT * FROM dim_session").fetchone()
        self.assertEqual(row["first_ts"], "2026-06-29T08:00:00.000Z")
        self.assertEqual(row["last_ts"], "2026-06-29T12:00:00.000Z")
        before = row["updated_at"]
        self.ingest([rec("d", ts_utc="2026-06-29T09:00:00.000Z")])   # inside the span
        after = self.store.con.execute("SELECT updated_at FROM dim_session").fetchone()[0]
        self.assertEqual(after, before, "a ts inside the span must not touch the row")

    def test_account_uuid_is_attached_from_the_source(self):
        self.ingest([rec("a")])
        self.assertEqual(self.row("a")["account_uuid"], "acct-1")


class ScanStateTests(StoreTestCase):
    def test_pruned_file_is_flagged_but_rows_are_kept(self):
        """PLAN.md §13 invariant 5: a missing source file never removes a row."""
        self.ingest([rec("a")])
        self.store.record_scan("host", "/root/s.jsonl", 10, 1.0, 10, 3, "sha", "u", "full")
        self.store.mark_deleted("host", ["/root/s.jsonl"])
        self.store.con.commit()
        state = self.store.con.execute("SELECT * FROM scan_state").fetchone()
        self.assertIsNotNone(state["deleted_at"])
        self.assertEqual(state["last_result"], "deleted")
        self.assertIsNotNone(self.row("a"), "row must outlive its source file")

    def test_load_scan_state_hides_deleted_files(self):
        self.store.record_scan("host", "/root/a.jsonl", 1, 1.0, 1, 1, "x", "u", "full")
        self.store.record_scan("host", "/root/b.jsonl", 1, 1.0, 1, 1, "x", "u", "full")
        self.store.mark_deleted("host", ["/root/b.jsonl"])
        self.assertEqual(list(self.store.load_scan_state("host")), ["/root/a.jsonl"])

    def test_rescanning_a_pruned_file_revives_it(self):
        self.store.record_scan("host", "/root/a.jsonl", 1, 1.0, 1, 1, "x", "u", "full")
        self.store.mark_deleted("host", ["/root/a.jsonl"])
        self.store.record_scan("host", "/root/a.jsonl", 2, 2.0, 2, 1, "y", "u", "full")
        self.assertIn("/root/a.jsonl", self.store.load_scan_state("host"))

    def test_watermark_round_trips(self):
        self.assertIsNone(self.store.watermark("host"))
        self.store.set_watermark("host", "2026-06-29T08:00:00.000Z")
        self.assertAlmostEqual(self.store.watermark("host"), 1782000000.0, delta=86400 * 400)


class SchemaTests(StoreTestCase):
    def test_init_schema_is_idempotent(self):
        self.store.init_schema()
        self.store.init_schema()
        self.assertEqual(self.store.schema_version(), SCHEMA_VERSION)

    def test_every_table_has_audit_columns(self):
        q = self.store.con.execute
        tables = [r[0] for r in q(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        self.assertGreater(len(tables), 5)
        for table in tables:
            cols = {r[1] for r in q(f"PRAGMA table_info({table})")}
            if table == "meta":
                continue
            self.assertIn("created_at", cols, f"{table} lacks created_at")
            self.assertIn("updated_at", cols, f"{table} lacks updated_at")

    def test_migration_converges_with_a_fresh_schema(self):
        """A migrated database and a fresh one must hold the same columns.

        Order differs by design -- ALTER TABLE appends, schema.sql places the
        column where it reads best -- and nothing depends on ordinal position
        because every statement binds by name.
        """
        fresh = {r[1] for r in self.store.con.execute("PRAGMA table_info(usage_event)")}
        d = tempfile.mkdtemp()
        with Store(os.path.join(d, "old.db")) as old:
            old.con.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);")
            old.init_schema()                      # creates v2 directly
            old.con.execute("ALTER TABLE usage_event DROP COLUMN speed")
            old.migrate()                          # must put it back
            migrated = {r[1] for r in old.con.execute("PRAGMA table_info(usage_event)")}
        self.assertEqual(fresh, migrated)
        self.assertIn("speed", migrated)

    def test_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.con.execute(
                "INSERT INTO dim_project(source_id, path) VALUES ('nope','/x')")

    def test_source_files_contain_no_insert_or_replace(self):
        """INSERT OR REPLACE would overwrite a complete row with a partial one.

        Matches the full `... INTO` form: prose may name the forbidden
        statement, but only real SQL names a table after it.
        """
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        for name in os.listdir(src):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(src, name), encoding="utf-8") as fh:
                text = " ".join(fh.read().upper().split())
            self.assertNotIn("INSERT OR REPLACE INTO", text,
                             f"{name} uses INSERT OR REPLACE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
