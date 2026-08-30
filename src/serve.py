"""Local dashboard server with a refresh endpoint.

The dashboard stays static. This module adds nothing the
page requires -- only something it can use when present. Over file://, or
behind any ordinary static host, `/api/health` does not answer, the refresh
button never appears, and the page behaves exactly as it does today.

Refreshing shells out to `python -m src run` rather than calling into the
package in process, which is deliberate on four counts:

  - it is the identical path Task Scheduler invokes, so the button cannot
    drift from the scheduled behaviour as either one changes;
  - stdout stays isolated. An in-process run would have to swap the
    process-global sys.stdout while a threaded server is writing to it;
  - runlog writes data/logs/YYYY-MM.log for free, so a click is exactly as
    visible after the fact as a scheduled run;
  - an ingest that dies cannot take the server down with it.

The cost is one interpreter start (~0.2 s) on top of a ~1.5 s incremental
run, which is not perceptible behind a button that already shows progress.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# A cross-origin page can POST to localhost without reading the response, so a
# site you happen to be visiting could otherwise trigger runs on your machine.
# Requiring a non-simple header forces a CORS preflight, which such a page
# cannot satisfy. Cheap, and proportionate to a single-user local tool.
REFRESH_HEADER = "X-TokenDiary"
REFRESH_TIMEOUT = 600           # generous: --full over the WSL redirector is ~7 s


def run_refresh(project_root: str, full: bool = False) -> tuple[int, str]:
    """Invoke `python -m src run` and return (exit code, combined output)."""
    argv = [sys.executable, "-m", "src", "run"] + (["--full"] if full else [])
    try:
        proc = subprocess.run(
            argv, cwd=project_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=REFRESH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 1, f"refresh timed out after {REFRESH_TIMEOUT}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def make_handler(web_dir: str, project_root: str):
    lock = threading.Lock()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=web_dir, **kw)

        # -- helpers ---------------------------------------------------

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def end_headers(self) -> None:
            # Serves the same purpose as the page's `cache: "no-store"` fetch,
            # from the other end: a refresh rewrites data.json in place, and a
            # heuristically cached copy would render the previous run's numbers
            # with nothing to show for it but a stale "updated" stamp.
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        # -- routes ----------------------------------------------------

        def do_GET(self) -> None:
            if self.path.split("?")[0] == "/api/health":
                return self._json(200, {"refresh": True})
            super().do_GET()

        def do_POST(self) -> None:
            if self.path.split("?")[0] != "/api/refresh":
                return self._json(404, {"error": "no such endpoint"})
            if REFRESH_HEADER not in self.headers:
                return self._json(403, {"error": f"missing {REFRESH_HEADER} header"})
            if not lock.acquire(blocking=False):
                return self._json(409, {"error": "a refresh is already running"})
            try:
                full = "full=1" in self.path
                code, output = run_refresh(project_root, full=full)
            finally:
                lock.release()

            # Exit 1 means a source was unavailable and the others still
            # ingested, so the data on disk IS newer -- the page must reload
            # it rather than treat this as a failure (D17).
            status = 200 if code <= 1 else 409 if code == 6 else 500
            self._json(status, {"exit_code": code, "output": output})

    return Handler


def make_server(project_root: str, port: int = 8899, bind: str = "127.0.0.1"):
    """Bound to loopback only: this endpoint runs a subprocess on request."""
    handler = make_handler(f"{project_root}/web", project_root)
    return ThreadingHTTPServer((bind, port), handler)
