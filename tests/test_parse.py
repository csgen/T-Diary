"""Tests for the M0 parse layer.

Focus on the rules that are invisible when broken: dedup, partial-snapshot
merging, torn-line handling, and UTC -> UTC+8 day boundaries.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.parse import UsageRecord, is_mirror, merge_records, parse_file  # noqa: E402

TZ8 = timezone(timedelta(hours=8))


def line(msg_id, out, ts="2026-06-29T08:00:00.000Z", model="claude-opus-5", **extra):
    obj = {
        "type": "assistant",
        "uuid": f"u-{msg_id}-{out}",
        "requestId": f"req_{msg_id}",
        "timestamp": ts,
        "sessionId": "sess-1",
        "cwd": "/home/dev/proj",
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": 10,
                "output_tokens": out,
                "output_tokens_details": {"thinking_tokens": 3},
                "cache_read_input_tokens": 500,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 20,
                    "ephemeral_1h_input_tokens": 40,
                },
                "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 0},
                "service_tier": "standard",
            },
        },
    }
    obj.update(extra)
    return json.dumps(obj) + "\n"


def write(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "wb") as fh:
        fh.write(content.encode("utf-8"))
    return path.replace("\\", "/")


class ParseFileTests(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "_paths", []):
            try:
                os.unlink(p)
            except OSError:
                pass

    def parse(self, content, **kw):
        path = write(content)
        self._paths = getattr(self, "_paths", []) + [path]
        return parse_file(path, "test", "proj/sess.jsonl", TZ8, **kw)

    def test_dedup_keeps_max_output_tokens(self):
        """Streaming snapshots collapse to one record at the final count."""
        res = self.parse(line("msg_a", 3) + line("msg_a", 114) + line("msg_a", 114))
        self.assertEqual(len(res.records), 1)
        self.assertEqual(res.records[0].output_tokens, 114)
        self.assertEqual(res.stats.usage_lines, 3)
        self.assertEqual(res.stats.collapsed, 2)

    def test_dedup_ignores_line_order(self):
        """Final-then-partial must not regress the count."""
        res = self.parse(line("msg_a", 114) + line("msg_a", 3))
        self.assertEqual(res.records[0].output_tokens, 114)

    def test_synthetic_model_excluded(self):
        res = self.parse(line("msg_a", 50, model="<synthetic>") + line("msg_b", 7))
        self.assertEqual([r.message_id for r in res.records], ["msg_b"])
        self.assertEqual(res.stats.skipped_synthetic, 1)

    def test_lines_without_usage_are_cheap_and_ignored(self):
        res = self.parse('{"type":"user","message":{"role":"user"}}\n' + line("msg_a", 5))
        self.assertEqual(len(res.records), 1)
        self.assertEqual(res.stats.lines_read, 2)
        self.assertEqual(res.stats.usage_lines, 1)

    def test_torn_trailing_line_is_not_consumed(self):
        """A half-written line must be left for the next scan, not parsed."""
        content = line("msg_a", 5)
        torn = line("msg_b", 9)[:40]
        res = self.parse(content + torn)
        self.assertTrue(res.stats.torn_tail)
        self.assertEqual(res.byte_offset, len(content.encode()))
        self.assertEqual([r.message_id for r in res.records], ["msg_a"])

    def test_offset_resume_reads_only_new_bytes(self):
        first = line("msg_a", 5)
        res = self.parse(first + line("msg_b", 9), start_offset=len(first.encode()))
        self.assertEqual([r.message_id for r in res.records], ["msg_b"])

    def test_offset_is_a_byte_count_not_a_character_count(self):
        """Non-ASCII must not desynchronize the offset (CJK is 3 bytes in UTF-8)."""
        first = line("msg_a", 5, gitBranch="功能/中文分支")
        res = self.parse(first + line("msg_b", 9))
        self.assertEqual(res.byte_offset, len((first + line("msg_b", 9)).encode("utf-8")))
        resumed = self.parse(first + line("msg_b", 9), start_offset=len(first.encode("utf-8")))
        self.assertEqual([r.message_id for r in resumed.records], ["msg_b"])

    def test_all_usage_components_captured(self):
        rec = self.parse(line("msg_a", 114)).records[0]
        self.assertEqual(
            (rec.input_tokens, rec.output_tokens, rec.thinking_tokens,
             rec.cache_read_tokens, rec.cache_write_5m_tokens,
             rec.cache_write_1h_tokens, rec.web_search_requests),
            (10, 114, 3, 500, 20, 40, 1),
        )

    def test_utc_to_local_day_boundary(self):
        """16:21 UTC is the next calendar day at UTC+8 -- 2.5% of real events."""
        rec = self.parse(line("msg_a", 5, ts="2026-06-29T16:21:37.893Z")).records[0]
        self.assertEqual(rec.ts_utc, "2026-06-29T16:21:37.893Z")   # stored verbatim
        self.assertEqual(rec.local_date, "2026-06-30")
        self.assertEqual(rec.local_hour, 0)
        self.assertEqual(rec.month, "2026-06")

    def test_iso_week_spans_year_boundary(self):
        rec = self.parse(line("msg_a", 5, ts="2026-01-01T00:00:00.000Z")).records[0]
        self.assertEqual(rec.local_date, "2026-01-01")
        self.assertEqual(rec.iso_week, "2026-W01")

    def test_missing_message_id_falls_back_to_request_id(self):
        obj = json.loads(line("msg_a", 5))
        del obj["message"]["id"]
        res = self.parse(json.dumps(obj) + "\n")
        self.assertEqual(res.records[0].message_id, "req_msg_a")

    def test_bad_json_is_counted_not_fatal(self):
        res = self.parse('{"message":{"usage":{ broken\n' + line("msg_a", 5))
        self.assertEqual(res.stats.bad_json, 1)
        self.assertEqual(len(res.records), 1)


class MergeTests(unittest.TestCase):
    def rec(self, source_id, rel_path, out=100, mid="msg_x"):
        return UsageRecord(
            message_id=mid, request_id=None, source_id=source_id, session_id="s",
            project_path="/home/dev/proj", rel_path=rel_path, git_branch=None,
            entrypoint=None, model="claude-opus-4-8", effort=None, service_tier=None,
            is_sidechain=0, agent_id=None, ts_utc="2026-08-11T09:26:47.000Z",
            local_date="2026-08-11", local_hour=17, iso_week="2026-W33",
            month="2026-08", year=2026, input_tokens=1, output_tokens=out,
            thinking_tokens=0, cache_read_tokens=0, cache_write_5m_tokens=0,
            cache_write_1h_tokens=0, web_search_requests=0, web_fetch_requests=0,
        )

    def test_ssh_mirror_is_recognized(self):
        self.assertTrue(is_mirror(self.rec("client", "ssh-50bb3d76/sess.jsonl")))
        self.assertFalse(is_mirror(self.rec("host", "-home-dev-proj/sess.jsonl")))

    def test_original_beats_mirror_regardless_of_order(self):
        """The host that ran the session owns the call, not the SSH client."""
        mirror = self.rec("client", "ssh-50bb3d76/sess.jsonl")
        original = self.rec("host", "-home-dev-proj/sess.jsonl")
        for order in ([mirror, original], [original, mirror]):
            merged = merge_records(order)
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged["msg_x"].source_id, "host")

    def test_collisions_are_reported(self):
        collisions: list = []
        merge_records(
            [self.rec("client", "ssh-x/s.jsonl"), self.rec("host", "-home/s.jsonl")],
            collisions=collisions,
        )
        self.assertEqual(len(collisions), 1)

    def test_higher_output_wins_over_mirror_preference(self):
        """A more complete snapshot beats provenance -- completeness first."""
        mirror = self.rec("client", "ssh-x/s.jsonl", out=500)
        original = self.rec("host", "-home/s.jsonl", out=100)
        self.assertEqual(merge_records([original, mirror])["msg_x"].output_tokens, 500)

    def test_distinct_ids_are_kept_apart(self):
        merged = merge_records(
            [self.rec("host", "-home/s.jsonl", mid="a"), self.rec("host", "-home/s.jsonl", mid="b")]
        )
        self.assertEqual(sorted(merged), ["a", "b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
