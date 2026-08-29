"""Local-time derivation from a recorded offset history.

The JSONL carries only UTC -- every timestamp ends in `Z`, with no timezone
information anywhere. So "what day was this?" is a policy decision, and the
policy has to be recorded somewhere durable.

`timezone.json` is that record, and it is the ONLY input to any date
calculation.

Periods are appended automatically when a scan observes a different machine
offset, which picks up both travel and DST without anyone maintaining a list.
They stay hand-editable, and `origin: "manual"` marks an entry a human fixed so
a later scan does not argue with it.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from dataclasses import dataclass, field

TZ_NAME = "timezone.json"

DEFAULT_NOTE = [
    "Offset history used to turn the UTC timestamps in Claude's JSONL into local dates.",
    "This file is the ONLY input to that calculation -- the database holds the result,",
    "not the rule. Losing it means stored dates can no longer be rebuilt, so it is",
    "worth keeping a copy.",
    "",
    "Periods are appended automatically when a scan sees a different machine offset",
    "(travel or DST). Edit anything here freely, including this note: a rewrite only",
    "replaces 'periods' and preserves every other key. Set origin to 'manual' on an",
    "entry you correct so a later scan leaves it alone, then run `rebuild-dates` to",
    "apply the change to rows already stored.",
]
FLOOR = "2000-01-01T00:00:00Z"


class TimezoneError(Exception):
    """timezone.json is missing, malformed, or internally inconsistent."""


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def machine_offset() -> tuple[int, str]:
    """The local machine's current UTC offset in minutes, and its name.

    Minutes rather than hours because half-hour and quarter-hour zones are
    real: India +5:30, Nepal +5:45, Chatham +12:45.
    """
    local = datetime.datetime.now().astimezone()
    offset = local.utcoffset() or datetime.timedelta(0)
    return int(offset.total_seconds() // 60), (local.tzname() or "")


def derive(ts_utc: str, offset_minutes: int) -> dict:
    """UTC timestamp + offset -> the derived time columns.

    `ts_utc` is stored verbatim and never changes; everything here is a cache
    of a policy decision and can be rebuilt at any time.
    """
    utc = datetime.datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=datetime.timezone.utc)
    local = utc.astimezone(datetime.timezone(datetime.timedelta(minutes=offset_minutes)))
    iso = local.isocalendar()
    return {
        "local_date": local.strftime("%Y-%m-%d"),
        "local_hour": local.hour,
        "iso_week": f"{iso[0]}-W{iso[1]:02d}",
        "month": local.strftime("%Y-%m"),
        "year": local.year,
        "tz_offset_minutes": offset_minutes,
    }


@dataclass(slots=True)
class FixedOffset:
    """A single unchanging offset. Used by tests and as the seed for a new file."""

    offset_minutes: int

    def derive(self, ts_utc: str) -> dict:
        return derive(ts_utc, self.offset_minutes)


@dataclass(slots=True)
class Period:
    from_utc: str
    offset_minutes: int
    tz_name: str = ""
    origin: str = "auto"          # 'config' | 'auto' | 'manual'
    observed_at: str = ""

    def to_json(self) -> dict:
        return {"from": self.from_utc, "offset_minutes": self.offset_minutes,
                "tz_name": self.tz_name, "origin": self.origin,
                "observed_at": self.observed_at}

    @property
    def label(self) -> str:
        sign = "+" if self.offset_minutes >= 0 else "-"
        h, m = divmod(abs(self.offset_minutes), 60)
        return f"UTC{sign}{h}:{m:02d}"


@dataclass(slots=True)
class TimezoneHistory:
    periods: list[Period] = field(default_factory=list)
    path: str = ""
    extra: dict = field(default_factory=dict)
    """Every top-level key except `periods`, kept verbatim.

    A scan rewrites this file whenever it appends a period, so anything the
    file holds that the code does not own -- the explanatory note, a comment
    key someone added, a reminder to themselves -- has to survive that
    rewrite. Only `periods` is ours to replace.
    """

    def resolve(self, ts_utc: str) -> Period:
        """The period in force at `ts_utc`.

        A timestamp before the earliest period falls back to that period rather
        than failing: the floor entry starts at 2000-01-01, so this only
        happens with a hand-edited file, and guessing the oldest known offset
        beats refusing to date the row at all.
        """
        if not self.periods:
            raise TimezoneError(f"{self.path}: no periods")
        chosen = self.periods[0]
        for p in self.periods:
            if p.from_utc <= ts_utc:
                chosen = p
            else:
                break
        return chosen

    def derive(self, ts_utc: str) -> dict:
        return derive(ts_utc, self.resolve(ts_utc).offset_minutes)

    @property
    def current(self) -> Period:
        return self.periods[-1]


def load_history(root_dir: str, seed_offset_minutes: int | None = None) -> TimezoneHistory:
    """Read timezone.json, creating it from the config seed when absent."""
    path = f"{root_dir}/{TZ_NAME}"
    if not os.path.exists(path):
        if seed_offset_minutes is None:
            raise TimezoneError(f"no {TZ_NAME} at {path} and no seed offset given")
        name = machine_offset()[1]
        history = TimezoneHistory(
            [Period(FLOOR, seed_offset_minutes, name, "config", _utc_now())],
            path, {"_note": DEFAULT_NOTE})
        save_history(history)
        return history

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise TimezoneError(f"{path}: invalid JSON ({exc})") from exc

    raw = data.get("periods")
    if not isinstance(raw, list) or not raw:
        raise TimezoneError(f"{path}: needs a non-empty 'periods' list")

    periods = []
    for i, p in enumerate(raw):
        if "from" not in p or "offset_minutes" not in p:
            raise TimezoneError(f"{path}: periods[{i}] needs 'from' and 'offset_minutes'")
        try:
            minutes = int(p["offset_minutes"])
        except (TypeError, ValueError):
            raise TimezoneError(f"{path}: periods[{i}] offset_minutes must be an integer")
        if not -14 * 60 <= minutes <= 14 * 60:
            raise TimezoneError(f"{path}: periods[{i}] offset {minutes} min is out of range")
        periods.append(Period(str(p["from"]), minutes, p.get("tz_name") or "",
                              p.get("origin") or "manual", p.get("observed_at") or ""))

    periods.sort(key=lambda p: p.from_utc)
    for a, b in zip(periods, periods[1:]):
        if a.from_utc == b.from_utc:
            raise TimezoneError(f"{path}: two periods start at {a.from_utc}")
    extra = {k: v for k, v in data.items() if k != "periods"}
    return TimezoneHistory(periods, path, extra)


def save_history(history: TimezoneHistory) -> None:
    """Write atomically: a half-written file would take the dates with it.

    Only `periods` is replaced. Every other top-level key is written back
    exactly as it was read, so hand-edited notes survive an auto-append.
    Formatting is normalized to 2-space indent -- the file is parsed as a whole
    document and rewritten, never patched line by line.
    """
    payload = {**history.extra, "periods": [p.to_json() for p in history.periods]}
    tmp = history.path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, history.path)


def append_if_changed(history: TimezoneHistory, offset_minutes: int | None = None,
                      tz_name: str | None = None, now_utc: str | None = None) -> Period | None:
    """Record a new period when the machine offset differs from the latest one.

    Returns the appended period, or None when nothing changed -- which is the
    normal case on almost every scan.

    The transition is stamped at observation time, so accuracy equals the scan
    interval: land at 14:00 and scan at 22:00 and the seam sits at 22:00. A
    tighter schedule tightens the bound; the boundary can also be corrected by
    hand afterwards.
    """
    if offset_minutes is None:
        offset_minutes, detected = machine_offset()
        tz_name = tz_name if tz_name is not None else detected
    if history.current.offset_minutes == offset_minutes:
        return None

    now = now_utc or _utc_now()
    period = Period(now, offset_minutes, tz_name or "", "auto", now)
    history.periods.append(period)
    save_history(history)
    return period
