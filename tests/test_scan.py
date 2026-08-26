"""Tests for the M0 scan layer -- candidate selection and deletion detection.

The sweep itself is exercised against the real filesystem by `python -m src
scan`; these cover the decision logic, which has no I/O and is where the
watermark / hot-window rules can silently go wrong.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config  # noqa: E402
from src.scan import FileStat, detect_deleted, select_candidates  # noqa: E402

NOW = 1_800_000_000.0
HOUR = 3600.0
SLACK = config.DEFAULT_WATERMARK_SLACK_SECONDS
HOT = config.DEFAULT_HOT_WINDOW_HOURS


def fs(name, age_hours, size=1000):
    return FileStat(
        source_id="host",
        path=f"/root/{name}.jsonl",
        rel_path=f"{name}.jsonl",
        size=size,
        mtime=NOW - age_hours * HOUR,
    )


def select(stats, state=None, watermark=None, **kw):
    return select_candidates(
        stats, state or {}, watermark,
        slack_seconds=SLACK, hot_window_hours=HOT, now=NOW, **kw,
    )


class SelectCandidateTests(unittest.TestCase):
    def test_first_run_selects_everything(self):
        """No watermark and no state means nothing has ever been read."""
        got = select([fs("a", 1), fs("b", 500)])
        self.assertEqual([c.reason for c in got], ["new", "new"])

    def test_unchanged_cold_file_is_skipped(self):
        old = fs("a", 500)
        state = {old.path: {"size": old.size}}
        self.assertEqual(select([old], state, watermark=NOW - HOUR), [])

    def test_changed_since_watermark_is_selected(self):
        f = fs("a", 0.5)                      # modified 30 min ago
        state = {f.path: {"size": f.size}}
        got = select([f], state, watermark=NOW - HOUR)
        self.assertEqual(got[0].reason, "mtime")

    def test_size_change_selects_even_when_mtime_looks_old(self):
        """mtime can lie -- restored backups, copies preserving timestamps."""
        f = fs("a", 500, size=2000)
        state = {f.path: {"size": 1000}}      # we recorded a different size
        got = select([f], state, watermark=NOW - HOUR)
        self.assertEqual(got[0].reason, "size")

    def test_slack_window_catches_a_file_written_during_the_last_scan(self):
        """Written 1 min after the scan started; without slack it is missed."""
        f = fs("a", 0)
        f.mtime = NOW - HOUR + 60
        state = {f.path: {"size": f.size}}
        got = select([f], state, watermark=NOW - HOUR + 120)
        self.assertEqual(got[0].reason, "mtime")

    def test_hot_flag_tracks_the_window_not_the_selection_reason(self):
        recent, old = fs("recent", HOT - 1), fs("old", HOT + 1)
        got = select([recent, old])
        self.assertEqual([c.hot for c in got], [True, False])

    def test_cold_unchanged_file_inside_hot_window_is_still_selected(self):
        """A file can be untouched since the watermark yet still be hot."""
        f = fs("a", 1)
        state = {f.path: {"size": f.size}}
        got = select([f], state, watermark=NOW)     # watermark ahead of mtime
        self.assertEqual(got[0].reason, "hot")
        self.assertTrue(got[0].hot)

    def test_force_full_overrides_every_skip(self):
        f = fs("a", 500)
        state = {f.path: {"size": f.size}}
        got = select([f], state, watermark=NOW, force_full=True)
        self.assertEqual(got[0].reason, "full")

    def test_candidate_carries_the_file(self):
        got = select([fs("a", 1)])
        self.assertEqual(got[0].file.rel_path, "a.jsonl")


class DetectDeletedTests(unittest.TestCase):
    def test_pruned_file_is_reported_once(self):
        """Claude pruning a session file is detected, never acted on destructively."""
        state = {"/root/gone.jsonl": {"size": 1}, "/root/here.jsonl": {"size": 1}}
        self.assertEqual(detect_deleted([fs("here", 1)], state), ["/root/gone.jsonl"])

    def test_already_flagged_deletion_is_not_reported_again(self):
        state = {"/root/gone.jsonl": {"size": 1, "deleted_at": "2026-08-25T00:00:00Z"}}
        self.assertEqual(detect_deleted([], state), [])


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.toml")


class TempConfigCase(unittest.TestCase):
    """Base for tests that load the real config.toml with a synthetic .env.

    The repository's own .env is gitignored and absent on a fresh clone and in
    CI, so no test may depend on it. Each case copies the committed config.toml
    into a temp directory beside an .env it controls, and hides any TD_* already
    in the environment so a developer's shell cannot change the outcome.
    """

    def setUp(self):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            self.cfg_text = fh.read()
        self._saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("TD_")}

    def tearDown(self):
        os.environ.update(self._saved)

    def load(self, env_text=""):
        d = tempfile.mkdtemp()
        with open(f"{d}/config.toml", "w", encoding="utf-8") as fh:
            fh.write(self.cfg_text)
        with open(f"{d}/.env", "w", encoding="utf-8") as fh:
            fh.write("TD_DASHBOARD_FROM=2026-01-01\n" + env_text)
        return config.load_config(f"{d}/config.toml")


class ConfigDefaultTests(TempConfigCase):
    def test_tuning_values_have_exactly_one_home_in_code(self):
        """config.toml owns these; code fallbacks live only in config.DEFAULT_*."""
        cfg = config.Config(sources=[])
        self.assertEqual(cfg.hot_window_hours, config.DEFAULT_HOT_WINDOW_HOURS)
        self.assertEqual(cfg.watermark_slack_seconds, config.DEFAULT_WATERMARK_SLACK_SECONDS)
        self.assertEqual(cfg.timezone_offset_hours, config.DEFAULT_TZ_OFFSET_HOURS)

    def test_config_toml_values_reach_the_config_object(self):
        cfg = self.load("TD_S1_ID=a\nTD_S1_PATH=/x\n")
        self.assertEqual(cfg.hot_window_hours, 48)
        self.assertEqual(cfg.watermark_slack_seconds, 300)
        self.assertEqual(cfg.tz.utcoffset(None).total_seconds() / 3600, 8)


class SourceSlotTests(TempConfigCase):
    """config.toml must never reveal which accounts exist, or how many.

    Slots are anonymous and filled from .env; unset slots are ignored.
    """

    def test_single_slot_is_enough(self):
        cfg = self.load("TD_S1_ID=solo\nTD_S1_PATH=/x/y\n")
        self.assertEqual([s.id for s in cfg.sources], ["solo"])
        self.assertEqual(cfg.unused_source_slots, 3)

    def test_slots_need_not_be_contiguous(self):
        cfg = self.load("TD_S1_ID=a\nTD_S1_PATH=/x\nTD_S3_ID=c\nTD_S3_PATH=/z\n")
        self.assertEqual([s.id for s in cfg.sources], ["a", "c"])

    def test_label_defaults_to_id_when_blank(self):
        cfg = self.load("TD_S1_ID=solo\nTD_S1_PATH=/x\n")
        self.assertEqual(cfg.sources[0].label, "solo")

    def test_blank_email_becomes_none_not_empty_string(self):
        cfg = self.load("TD_S1_ID=solo\nTD_S1_PATH=/x\nTD_S1_EMAIL=\n")
        self.assertIsNone(cfg.sources[0].account_hint)

    def test_half_filled_slot_is_rejected_not_skipped(self):
        """Silently dropping a source would render as 'you did no work'."""
        with self.assertRaises(config.ConfigError) as ctx:
            self.load("TD_S1_ID=oops\n")
        self.assertIn("slot 1", str(ctx.exception))

    def test_no_slots_configured_is_an_error(self):
        with self.assertRaises(config.ConfigError):
            self.load("")

    def test_duplicate_ids_are_rejected(self):
        with self.assertRaises(config.ConfigError):
            self.load("TD_S1_ID=dup\nTD_S1_PATH=/x\nTD_S2_ID=dup\nTD_S2_PATH=/y\n")

    def test_unresolved_variable_outside_sources_is_fatal(self):
        """An unset slot is fine; an unset tuning value is a typo."""
        d = tempfile.mkdtemp()
        with open(f"{d}/config.toml", "w", encoding="utf-8") as fh:
            fh.write(self.cfg_text)
        with open(f"{d}/.env", "w", encoding="utf-8") as fh:
            fh.write("TD_S1_ID=a\nTD_S1_PATH=/x\n")     # no TD_DASHBOARD_FROM
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(f"{d}/config.toml")
        self.assertIn("TD_DASHBOARD_FROM", str(ctx.exception))

    def test_real_environment_overrides_env_file(self):
        os.environ["TD_S1_ID"] = "from-shell"
        try:
            cfg = self.load("TD_S1_ID=from-file\nTD_S1_PATH=/x\n")
            self.assertEqual(cfg.sources[0].id, "from-shell")
        finally:
            os.environ.pop("TD_S1_ID", None)

    def test_committed_config_names_no_account(self):
        """The guard for this whole design: config.toml stays anonymous.

        Checks settings only, not comments -- prose may legitimately use words
        like "personal" while no value names an actual account.
        """
        settings = "\n".join(
            line.split("#", 1)[0] for line in self.cfg_text.splitlines()
        ).lower()
        for leaked in ("personal", "work", "@", "users/", "/home/", "wsl.", "c:/"):
            self.assertNotIn(leaked, settings, f"config.toml leaks {leaked!r}")

    def test_every_source_value_is_a_variable(self):
        """No source field may be hardcoded -- all four come from .env."""
        for line in self.cfg_text.splitlines():
            setting = line.split("#", 1)[0].strip()
            if not setting.startswith(("id ", "label ", "path ", "account_hint ")):
                continue
            value = setting.split("=", 1)[1].strip()
            self.assertRegex(value, r'^"\$\{[A-Z0-9_]+\}"$', f"hardcoded: {setting}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
