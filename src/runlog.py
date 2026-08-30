"""Run logging for unattended (scheduled) invocations.

A scheduled run has no console anyone is watching, so `run` tees everything
the commands already print into ``data/logs/YYYY-MM.log``.
The existing ``print()`` call sites are the log, so
the file reads exactly like the terminal output it replaces.

Task Scheduler surfaces the exit code as "Last Run Result" but never alerts
on it, so the log is the only place a failed night becomes noticeable
without going looking. Every run writes a banner and a closing exit line,
which is what makes it greppable.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import sys

LOG_ENCODING = "utf-8"


def log_path(root_dir: str, now: datetime.datetime | None = None) -> str:
    """``data/logs/YYYY-MM.log``, rotated on the UTC month.

    UTC time.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return f"{root_dir}/data/logs/{now:%Y-%m}.log"


class Tee:
    """Write to a console stream and the log file at once.

    The console is the fragile half. Under a scheduled task with stdout
    redirected on a legacy codepage, one non-ASCII character in a print()
    raises UnicodeEncodeError and kills a run that had otherwise finished
    its work. The log is always UTF-8; the console gets an ASCII fallback
    instead of an exception, so output can never be what fails a run.
    """

    def __init__(self, console, logfile):
        self.console = console
        self.logfile = logfile

    def write(self, text: str) -> int:
        self.logfile.write(text)
        try:
            self.console.write(text)
        except UnicodeEncodeError:
            self.console.write(text.encode("ascii", "replace").decode("ascii"))
        return len(text)

    def flush(self) -> None:
        self.logfile.flush()
        with contextlib.suppress(ValueError, OSError):
            self.console.flush()

    def isatty(self) -> bool:
        # A log file is never a terminal. Anything that colours or animates
        # its output should see a pipe, not a console.
        return False


@contextlib.contextmanager
def tee_to_log(root_dir: str, label: str, now: datetime.datetime | None = None):
    """Redirect stdout and stderr into the month's log for the duration.

    Both streams go to the same file so the interleaving matches what the
    terminal would have shown; a warning on stderr keeps its position
    relative to the line it followed.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    path = log_path(root_dir, now)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    handle = open(path, "a", encoding=LOG_ENCODING)
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = Tee(saved_out, handle)
    sys.stderr = Tee(saved_err, handle)
    try:
        print(f"\n=== {now:%Y-%m-%dT%H:%M:%SZ}  {label} ===")
        yield path
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
        handle.close()
