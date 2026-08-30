"""Tests for the M6 refresh endpoint.

Two properties matter here. The page must stay a static asset -- everything
the server adds is optional, and its absence is not an error. And the refresh
button must inherit `run`'s exit-code rule rather than restating it: exit 1
means the data on disk IS newer, so the page has to reload it (D17).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import serve as serve_mod  # noqa: E402


class ServeTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "web"))
        with open(os.path.join(self.tmp.name, "web", "data.json"), "w") as fh:
            json.dump({"meta": {"generated_at": "2026-08-31T00:00:00Z"}}, fh)

        self.refreshes = []
        self.outcome = (0, "inserted 3")
        self._saved = serve_mod.run_refresh
        serve_mod.run_refresh = self._fake_refresh
        self.addCleanup(lambda: setattr(serve_mod, "run_refresh", self._saved))

        self.httpd = serve_mod.make_server(self.tmp.name, port=0)
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _fake_refresh(self, project_root, full=False):
        self.refreshes.append(full)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def get(self, path, method="GET", headers=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                body = json.loads(body or b"{}")
            except ValueError:
                body = {}
            return e.code, body, dict(e.headers)

    # -- feature detection ----------------------------------------------

    def test_health_advertises_refresh(self):
        status, body, _ = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["refresh"])

    def test_static_files_are_still_served(self):
        status, body, _ = self.get("/data.json")
        self.assertEqual(status, 200)
        self.assertIn("meta", body)

    def test_every_response_forbids_caching(self):
        # The mirror of the page's `cache: "no-store"` fetch. A refresh
        # rewrites data.json in place, so a heuristically cached copy would
        # render the previous run's numbers.
        for path in ("/data.json", "/api/health"):
            _, _, headers = self.get(path)
            self.assertEqual(headers.get("Cache-Control"), "no-store", path)

    # -- the D17 rule, inherited rather than restated ---------------------

    def test_clean_run_returns_200(self):
        self.outcome = (0, "inserted 12")
        status, body, _ = self.get("/api/refresh", "POST", {"X-TokenDiary": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["exit_code"], 0)

    def test_partial_failure_still_reports_success_so_the_page_reloads(self):
        self.outcome = (1, "1 source(s) unavailable")
        status, body, _ = self.get("/api/refresh", "POST", {"X-TokenDiary": "1"})
        self.assertEqual(status, 200, "exit 1 means the data on disk is newer")
        self.assertEqual(body["exit_code"], 1)

    def test_lock_conflict_maps_to_409(self):
        self.outcome = (6, "another tokendiary run holds the database")
        status, body, _ = self.get("/api/refresh", "POST", {"X-TokenDiary": "1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["exit_code"], 6)

    def test_hard_failure_maps_to_500(self):
        self.outcome = (4, "price error")
        status, _, _ = self.get("/api/refresh", "POST", {"X-TokenDiary": "1"})
        self.assertEqual(status, 500)

    def test_full_is_opt_in(self):
        self.get("/api/refresh", "POST", {"X-TokenDiary": "1"})
        self.get("/api/refresh?full=1", "POST", {"X-TokenDiary": "1"})
        self.assertEqual(self.refreshes, [False, True])

    # -- guards -----------------------------------------------------------

    def test_refresh_requires_the_custom_header(self):
        # Without this a page you happen to be visiting could POST here: a
        # cross-origin request cannot read the response, but it can send one.
        status, _, _ = self.get("/api/refresh", "POST")
        self.assertEqual(status, 403)
        self.assertEqual(self.refreshes, [], "nothing ran")

    def test_unknown_endpoint_is_404(self):
        status, _, _ = self.get("/api/nope", "POST", {"X-TokenDiary": "1"})
        self.assertEqual(status, 404)

    def test_concurrent_refreshes_do_not_stack(self):
        started, release = threading.Event(), threading.Event()

        def slow(project_root, full=False):
            self.refreshes.append(full)
            started.set()
            release.wait(5)
            return 0, "done"

        serve_mod.run_refresh = slow
        results = {}
        t = threading.Thread(target=lambda: results.update(
            first=self.get("/api/refresh", "POST", {"X-TokenDiary": "1"})[0]))
        t.start()
        self.assertTrue(started.wait(5), "first refresh never started")
        second, body, _ = self.get("/api/refresh", "POST", {"X-TokenDiary": "1"})
        release.set()
        t.join(10)

        self.assertEqual(second, 409, "a second click must not start a second run")
        self.assertIn("already running", body["error"])
        self.assertEqual(results["first"], 200)
        self.assertEqual(len(self.refreshes), 1)


if __name__ == "__main__":
    unittest.main()
