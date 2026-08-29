"""Tests for the timezone layer.

`timezone.json` is the only input to date derivation, and the properties that
matter are: the same history produces the same dates anywhere, a scan records a
change rather than silently applying one, and a timezone change can never move
a cost.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.price import compute_cost, load_prices  # noqa: E402
from src.tz import (FLOOR, FixedOffset, Period, TimezoneError,  # noqa: E402
                    append_if_changed, derive, load_history, save_history)


def write_history(periods) -> str:
    d = tempfile.mkdtemp()
    with open(f"{d}/timezone.json", "w", encoding="utf-8") as fh:
        json.dump({"periods": periods}, fh)
    return d


SG = {"from": FLOOR, "offset_minutes": 480, "tz_name": "SGT",
      "origin": "config", "observed_at": ""}
FRA = {"from": "2026-08-15T10:00:00Z", "offset_minutes": 120, "tz_name": "CEST",
       "origin": "auto", "observed_at": ""}


class DeriveTests(unittest.TestCase):
    def test_offset_shifts_the_calendar_day(self):
        """16:21 UTC is the next day at UTC+8."""
        self.assertEqual(derive("2026-06-29T16:21:37.893Z", 480)["local_date"], "2026-06-30")
        self.assertEqual(derive("2026-06-29T16:21:37.893Z", 120)["local_date"], "2026-06-29")

    def test_half_hour_offsets_work(self):
        """India +5:30 and Nepal +5:45 are unrepresentable as integer hours."""
        self.assertEqual(derive("2026-06-29T18:40:00Z", 330)["local_date"], "2026-06-30")
        self.assertEqual(derive("2026-06-29T18:40:00Z", 345)["local_hour"], 0)

    def test_all_derived_fields_agree(self):
        t = derive("2026-01-01T00:00:00Z", 480)
        self.assertEqual((t["local_date"], t["iso_week"], t["month"], t["year"]),
                         ("2026-01-01", "2026-W01", "2026-01", 2026))
        self.assertEqual(t["tz_offset_minutes"], 480)

    def test_fixed_offset_clock(self):
        self.assertEqual(FixedOffset(480).derive("2026-06-29T16:00:00Z")["local_date"],
                         "2026-06-30")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.h = load_history(write_history([SG, FRA]))

    def test_period_in_force_is_the_latest_at_or_before(self):
        self.assertEqual(self.h.resolve("2026-08-15T09:59:00Z").offset_minutes, 480)
        self.assertEqual(self.h.resolve("2026-08-15T10:00:00Z").offset_minutes, 120)
        self.assertEqual(self.h.resolve("2026-09-01T00:00:00Z").offset_minutes, 120)

    def test_periods_are_sorted_regardless_of_file_order(self):
        h = load_history(write_history([FRA, SG]))
        self.assertEqual([p.offset_minutes for p in h.periods], [480, 120])

    def test_timestamp_before_every_period_uses_the_earliest(self):
        """Guessing the oldest known offset beats refusing to date the row."""
        h = load_history(write_history([FRA]))
        self.assertEqual(h.resolve("1999-01-01T00:00:00Z").offset_minutes, 120)

    def test_derivation_is_reproducible(self):
        """Same history, same answer -- wherever it is run."""
        a = load_history(write_history([SG, FRA]))
        b = load_history(write_history([SG, FRA]))
        ts = "2026-08-17T17:00:00Z"
        self.assertEqual(a.derive(ts), b.derive(ts))

    def test_a_trip_moves_only_the_days_inside_it(self):
        inside = self.h.derive("2026-08-17T17:00:00Z")     # Frankfurt window
        before = self.h.derive("2026-08-01T17:00:00Z")     # still Singapore
        self.assertEqual(inside["local_date"], "2026-08-17")
        self.assertEqual(before["local_date"], "2026-08-02")


class FileTests(unittest.TestCase):
    def test_missing_file_is_seeded_from_config(self):
        d = tempfile.mkdtemp()
        h = load_history(d, seed_offset_minutes=480)
        self.assertTrue(os.path.exists(f"{d}/timezone.json"))
        self.assertEqual(len(h.periods), 1)
        self.assertEqual((h.current.offset_minutes, h.current.origin), (480, "config"))

    def test_missing_file_without_a_seed_is_an_error(self):
        with self.assertRaises(TimezoneError):
            load_history(tempfile.mkdtemp())

    def test_empty_period_list_is_rejected(self):
        with self.assertRaises(TimezoneError):
            load_history(write_history([]))

    def test_out_of_range_offset_is_rejected(self):
        with self.assertRaises(TimezoneError):
            load_history(write_history([{**SG, "offset_minutes": 2000}]))

    def test_duplicate_start_is_rejected(self):
        """Two periods starting at the same instant have no defined winner."""
        with self.assertRaises(TimezoneError):
            load_history(write_history([SG, {**FRA, "from": FLOOR}]))

    def test_hand_edited_note_survives_an_auto_append(self):
        """A scan rewrites this file; anything the code does not own must survive."""
        d = tempfile.mkdtemp()
        with open(f"{d}/timezone.json", "w", encoding="utf-8") as fh:
            json.dump({"_note": ["my own words"], "_reminder": "Q3 offsite",
                       "periods": [SG]}, fh)
        h = load_history(d)
        append_if_changed(h, 120, "CEST", "2026-09-15T12:00:00Z")

        after = json.load(open(f"{d}/timezone.json", encoding="utf-8"))
        self.assertEqual(after["_note"], ["my own words"])
        self.assertEqual(after["_reminder"], "Q3 offsite")
        self.assertEqual(len(after["periods"]), 2)

    def test_only_periods_is_replaced(self):
        d = tempfile.mkdtemp()
        with open(f"{d}/timezone.json", "w", encoding="utf-8") as fh:
            json.dump({"anything": {"nested": [1, 2, 3]}, "periods": [SG]}, fh)
        h = load_history(d)
        save_history(h)
        after = json.load(open(f"{d}/timezone.json", encoding="utf-8"))
        self.assertEqual(after["anything"], {"nested": [1, 2, 3]})

    def test_a_new_file_gets_the_default_note(self):
        d = tempfile.mkdtemp()
        load_history(d, seed_offset_minutes=480)
        after = json.load(open(f"{d}/timezone.json", encoding="utf-8"))
        self.assertIn("_note", after)

    def test_save_then_load_round_trips(self):
        d = write_history([SG, FRA])
        h = load_history(d)
        save_history(h)
        again = load_history(d)
        self.assertEqual([p.offset_minutes for p in again.periods],
                         [p.offset_minutes for p in h.periods])


class AppendTests(unittest.TestCase):
    def setUp(self):
        self.dir = write_history([SG])
        self.h = load_history(self.dir)

    def test_same_offset_appends_nothing(self):
        """The normal case on almost every scan."""
        self.assertIsNone(append_if_changed(self.h, 480, "SGT", "2026-09-01T00:00:00Z"))
        self.assertEqual(len(self.h.periods), 1)

    def test_changed_offset_appends_one_period(self):
        p = append_if_changed(self.h, 120, "CEST", "2026-09-15T12:00:00Z")
        self.assertIsNotNone(p)
        self.assertEqual((p.offset_minutes, p.origin), (120, "auto"))
        self.assertEqual(len(load_history(self.dir).periods), 2)

    def test_returning_home_appends_again(self):
        append_if_changed(self.h, 120, "CEST", "2026-09-15T12:00:00Z")
        append_if_changed(self.h, 480, "SGT", "2026-09-22T06:00:00Z")
        self.assertEqual([p.offset_minutes for p in load_history(self.dir).periods],
                         [480, 120, 480])

    def test_the_appended_period_starts_at_observation_time(self):
        """Accuracy equals the scan interval; the seam is where we noticed."""
        p = append_if_changed(self.h, 120, "CEST", "2026-09-15T22:00:00Z")
        self.assertEqual(p.from_utc, "2026-09-15T22:00:00Z")
        self.assertEqual(p.observed_at, "2026-09-15T22:00:00Z")

    def test_append_is_persisted_not_just_in_memory(self):
        append_if_changed(self.h, 120, "CEST", "2026-09-15T12:00:00Z")
        self.assertEqual(len(load_history(self.dir).periods), 2)


class PriceDecouplingTests(unittest.TestCase):
    """A timezone change must never move a cost.

    Prices resolve against the UTC date, because a vendor rate change happens
    at one instant worldwide. Keying them to local_date would mean travel
    decided which rate applied to a call already made.
    """

    def setUp(self):
        d = tempfile.mkdtemp()
        json.dump({"rev": 1, "server_tools": {}, "models": {"m": [
            {"effective_from": "2000-01-01", "input": 1.0, "output": 10.0,
             "cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1},
            {"effective_from": "2026-08-18", "input": 1.0, "output": 99.0,
             "cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1},
        ]}}, open(f"{d}/prices.json", "w", encoding="utf-8"))
        self.prices = load_prices(d)

    def test_utc_date_decides_the_rate_not_the_local_date(self):
        # 17 Aug 17:00 UTC: 18 Aug in Singapore, still 17 Aug in Frankfurt.
        ts = "2026-08-17T17:00:00Z"
        sg = derive(ts, 480)["local_date"]
        fra = derive(ts, 120)["local_date"]
        self.assertEqual((sg, fra), ("2026-08-18", "2026-08-17"))

        by_utc = self.prices.resolve("m", ts[:10])
        self.assertEqual(by_utc.output, 10.0, "the UTC date is 2026-08-17, the old rate")

        # Had it keyed on local_date, the two offsets would disagree on the rate.
        self.assertNotEqual(self.prices.resolve("m", sg).output,
                            self.prices.resolve("m", fra).output)

    def test_cost_is_identical_under_either_offset(self):
        ts = "2026-08-17T17:00:00Z"
        rates = self.prices.resolve("m", ts[:10])
        row = {"input_tokens": 0, "output_tokens": 1_000_000, "thinking_tokens": 0,
               "cache_read_tokens": 0, "cache_write_5m_tokens": 0,
               "cache_write_1h_tokens": 0, "web_search_requests": 0,
               "web_fetch_requests": 0}
        cost, _ = compute_cost(row, rates, self.prices)
        self.assertEqual(cost, 10.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
