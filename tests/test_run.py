"""Tests for the scheduled entry point.

The rule this file exists to pin: `run` exports even when ingest exits 1.
Exit 1 means one source was unavailable *and the others ingested new data*,
so treating it as a failure would freeze the dashboard on exactly the days
it has something new to show.
"""

from __future__ import annotations

import argparse
import datetime
import io
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import cli  # noqa: E402
from src.price import PriceError  # noqa: E402
from src.runlog import Tee, log_path  # noqa: E402


class RunTestCase(unittest.TestCase):
    """Drives cmd_run with ingest and export stubbed out.

    The orchestration is the unit under test, not what ingest does; the real
    pipeline is covered by the other 147 tests.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

        self._saved = (cli.cmd_ingest, cli.cmd_export, cli._project_root)
        cli._project_root = lambda args: self.tmp.name
        self.addCleanup(self._restore)

    def _restore(self):
        cli.cmd_ingest, cli.cmd_export, cli._project_root = self._saved

    def stub(self, ingest, export=0):
        """Install ingest/export stubs. An int is returned, an exception raised."""
        def make(name, outcome):
            def fn(args):
                self.calls.append(name)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return fn
        cli.cmd_ingest = make("ingest", ingest)
        cli.cmd_export = make("export", export)

    def run_cmd(self, full=False):
        return cli.cmd_run(argparse.Namespace(config=None, full=full))

    def log_text(self):
        path = log_path(self.tmp.name)
        with io.open(path, encoding="utf-8") as fh:
            return fh.read()

    # -- the rule that motivated the whole subcommand -------------------

    def test_export_still_runs_when_one_source_was_unavailable(self):
        self.stub(ingest=1)
        code = self.run_cmd()
        self.assertEqual(self.calls, ["ingest", "export"])
        self.assertEqual(code, 1, "the partial failure must still surface")

    def test_clean_run_exports_and_succeeds(self):
        self.stub(ingest=0)
        self.assertEqual(self.run_cmd(), 0)
        self.assertEqual(self.calls, ["ingest", "export"])

    # -- hard failures: nothing was written, so nothing to export -------

    def test_export_skipped_when_ingest_fails_hard(self):
        self.stub(ingest=2)
        self.assertEqual(self.run_cmd(), 2)
        self.assertEqual(self.calls, ["ingest"])

    def test_price_error_maps_to_4_and_skips_export(self):
        self.stub(ingest=PriceError("rev 2 already recorded with different rates"))
        self.assertEqual(self.run_cmd(), 4)
        self.assertEqual(self.calls, ["ingest"])

    def test_locked_database_maps_to_6_and_skips_export(self):
        self.stub(ingest=sqlite3.OperationalError("database is locked"))
        self.assertEqual(self.run_cmd(), 6)
        self.assertEqual(self.calls, ["ingest"])
        self.assertIn("another tokendiary run", self.log_text())

    def test_unrelated_operational_error_is_not_swallowed(self):
        self.stub(ingest=sqlite3.OperationalError("no such table: usage_event"))
        with self.assertRaises(sqlite3.OperationalError):
            self.run_cmd()

    # -- the worst code seen wins ---------------------------------------

    def test_export_failure_propagates(self):
        self.stub(ingest=0, export=2)
        self.assertEqual(self.run_cmd(), 2)

    def test_worst_of_the_two_is_returned(self):
        self.stub(ingest=1, export=2)
        self.assertEqual(self.run_cmd(), 2)

    # -- logging ---------------------------------------------------------

    def test_run_writes_a_banner_and_an_exit_line(self):
        self.stub(ingest=0)
        self.run_cmd(full=True)
        text = self.log_text()
        self.assertIn("run --full", text)
        self.assertIn("run finished: exit 0", text)

    def test_log_appends_across_runs(self):
        self.stub(ingest=0)
        self.run_cmd()
        self.run_cmd()
        self.assertEqual(self.log_text().count("run finished"), 2)

    def test_streams_are_restored_afterwards(self):
        out, err = sys.stdout, sys.stderr
        self.stub(ingest=0)
        self.run_cmd()
        self.assertIs(sys.stdout, out)
        self.assertIs(sys.stderr, err)

    def test_streams_are_restored_even_when_a_command_raises(self):
        out = sys.stdout
        self.stub(ingest=sqlite3.OperationalError("no such table: x"))
        with self.assertRaises(sqlite3.OperationalError):
            self.run_cmd()
        self.assertIs(sys.stdout, out)


class LogPathTestCase(unittest.TestCase):

    def test_rotates_on_the_utc_month(self):
        stamp = datetime.datetime(2026, 9, 1, 3, 0, tzinfo=datetime.timezone.utc)
        self.assertTrue(log_path("/root", stamp).endswith("data/logs/2026-09.log"))

    def test_uses_utc_not_local_for_the_filename(self):
        # 2026-09-01 03:00 at UTC+8 is still August in UTC, and the file
        # follows UTC (PLAN 5.0: machine time is always UTC).
        stamp = datetime.datetime(2026, 8, 31, 19, 0, tzinfo=datetime.timezone.utc)
        self.assertTrue(log_path("/root", stamp).endswith("2026-08.log"))


class TeeTestCase(unittest.TestCase):
    """Output must never be the thing that fails a run."""

    class Cp1252Console:
        """Stands in for a console on a legacy codepage."""

        def __init__(self):
            self.text = ""

        def write(self, s):
            s.encode("cp1252")        # raises exactly where a real console would
            self.text += s
            return len(s)

        def flush(self):
            pass

    def test_writes_to_both_streams(self):
        console, log = self.Cp1252Console(), io.StringIO()
        Tee(console, log).write("hello")
        self.assertEqual(console.text, "hello")
        self.assertEqual(log.getvalue(), "hello")

    def test_non_ascii_falls_back_on_the_console_but_survives_in_the_log(self):
        # A CJK project path is the realistic trigger: an em-dash survives
        # cp1252, but nothing outside the codepage does.
        console, log = self.Cp1252Console(), io.StringIO()
        Tee(console, log).write("scanned \u65e5\u672c\u8a9e/project\n")
        self.assertIn("?", console.text, "console degrades rather than raising")
        self.assertIn("\u65e5\u672c\u8a9e", log.getvalue(),
                      "the log keeps the real characters")

    def test_reports_as_not_a_terminal(self):
        self.assertFalse(Tee(self.Cp1252Console(), io.StringIO()).isatty())


if __name__ == "__main__":
    unittest.main()
