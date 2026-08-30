"""Tests for the M4 export layer.

The dashboard is a static page over file://, so `data.json` is the entire
contract between the database and the browser. What matters: the aggregate
matches the rows it came from, the payload carries what the page needs to be
honest about provenance, and export never touches audit columns (invariant 6).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import export as export_mod  # noqa: E402
from src.parse import UsageRecord  # noqa: E402
from src.store import Store  # noqa: E402


class FakeSource:
    def __init__(self, sid="host", label="Host", path="/root"):
        self.id, self.label, self.path = sid, label, path


class FakeAccount:
    uuid, email, org_name = "acct-1", "a@example.com", "Org"


class FakeConfig:
    timezone_offset_hours = 8
    week_start = "monday"
    dashboard_default_from = "2026-06-17"

    @property
    def tz(self):
        import datetime
        return datetime.timezone(datetime.timedelta(hours=8))


def rec(mid, date="2026-06-29", out=100, cost=1.5, source="host",
        model="claude-opus-5", sidechain=0, **kw):
    base = dict(
        message_id=mid, request_id=None, source_id=source, session_id="s",
        project_path="/p", rel_path="p/s.jsonl", git_branch=None, entrypoint=None,
        model=model, effort=None, service_tier=None, speed="standard",
        is_sidechain=sidechain, agent_id=None, ts_utc=f"{date}T02:00:00.000Z",
        local_date=date, local_hour=10, iso_week="2026-W27", month=date[:7],
        year=int(date[:4]), tz_offset_minutes=480,
        input_tokens=10, output_tokens=out, thinking_tokens=0,
        cache_read_tokens=1000, cache_write_5m_tokens=5, cache_write_1h_tokens=50,
        web_search_requests=0, web_fetch_requests=0,
    )
    base.update(kw)
    return UsageRecord(**base)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.db"))
        self.store.init_schema()
        self.store.upsert_source(FakeSource(), FakeAccount())
        self.cfg = FakeConfig()

    def tearDown(self):
        self.store.close()

    def ingest(self, records, costs=None):
        batch = self.store.begin_batch("incremental", "[]")
        self.store.upsert_events(records, batch)
        self.store.con.execute(
            "INSERT INTO price_rev(rev, applied_at, content_hash, prices_json) "
            "VALUES (1,'2026-01-01T00:00:00Z','h','{}') ON CONFLICT(rev) DO NOTHING")
        for r in records:
            self.store.set_cost(r.message_id, (costs or {}).get(r.message_id, 1.5), "{}", 1)
        self.store.con.commit()

    def build(self):
        return export_mod.build(self.store, self.cfg)

    def test_daily_totals_match_the_rows(self):
        self.ingest([rec("a", out=100), rec("b", out=200)])
        p = self.build()
        self.assertEqual(sum(r["n"] for r in p["daily"]), 2)
        self.assertEqual(sum(r["o"] for r in p["daily"]), 300)
        self.assertAlmostEqual(sum(r["cost"] for r in p["daily"]), 3.0)

    def test_grain_splits_by_date_source_model_and_sidechain(self):
        self.ingest([
            rec("a", date="2026-06-29"),
            rec("b", date="2026-06-30"),
            rec("c", date="2026-06-30", model="claude-fable-5"),
            rec("d", date="2026-06-30", sidechain=1),
        ])
        p = self.build()
        self.assertEqual(len(p["daily"]), 4)
        keys = {(r["d"], r["m"], r["x"]) for r in p["daily"]}
        self.assertIn(("2026-06-30", "claude-fable-5", 0), keys)
        self.assertIn(("2026-06-30", "claude-opus-5", 1), keys)

    def test_sidechain_is_separable_not_merged(self):
        """The dashboard offers include / only / exclude, so the split must
        survive aggregation rather than being folded in."""
        self.ingest([rec("a", sidechain=0), rec("b", sidechain=1)])
        p = self.build()
        self.assertEqual({r["x"] for r in p["daily"]}, {0, 1})

    def test_meta_carries_what_the_page_needs_to_be_honest(self):
        self.ingest([rec("a")])
        meta = self.build()["meta"]
        for key in ("today", "tz_label", "coverage", "sources", "models",
                    "price_rev", "pruned_files", "cost_note", "generated_at"):
            self.assertIn(key, meta)
        self.assertEqual(meta["tz_label"], "UTC+8")
        self.assertEqual(meta["coverage"]["calls"], 1)

    def test_unpriced_rows_are_surfaced_not_hidden(self):
        """A NULL cost must be visible as unpriced, never summed as zero."""
        self.ingest([rec("a")])
        self.store.set_cost("a", None, None, None)
        self.store.con.commit()
        p = self.build()
        self.assertEqual(sum(r["unpriced"] for r in p["daily"]), 1)
        self.assertEqual(sum(r["cost"] for r in p["daily"]), 0.0)

    def test_export_reads_no_audit_columns(self):
        """Invariant 6: output must not depend on WHEN a scan ran."""
        src = export_mod.DAILY_SQL + export_mod.HOURLY_SQL
        for banned in ("updated_at", "created_at", "revision_count", "last_batch_id"):
            self.assertNotIn(banned, src)

    def test_hourly_buckets_are_present(self):
        self.ingest([rec("a")])
        p = self.build()
        self.assertTrue(p["hourly"])
        self.assertEqual(p["hourly"][0]["h"], 10)

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        self.ingest([rec("a")])
        out = os.path.join(self.dir, "web", "data.json")
        size = export_mod.write(self.build(), out)
        self.assertGreater(size, 0)
        self.assertFalse(os.path.exists(out + ".tmp"))
        with open(out, encoding="utf-8") as fh:
            self.assertIn("daily", json.load(fh))

    def test_rewriting_replaces_rather_than_appends(self):
        self.ingest([rec("a")])
        out = os.path.join(self.dir, "data.json")
        export_mod.write(self.build(), out)
        export_mod.write(self.build(), out)
        with open(out, encoding="utf-8") as fh:
            json.load(fh)          # still valid JSON, not two documents

    def test_empty_database_still_produces_a_valid_payload(self):
        p = self.build()
        self.assertEqual(p["daily"], [])
        self.assertEqual(p["meta"]["coverage"]["calls"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
