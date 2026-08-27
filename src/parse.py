"""Stage 3: JSONL -> usage records, deduplicated by message_id (PLAN.md §6.3).

One API call is written as several lines, one per content block, each repeating
the same cumulative `usage`. Counting per line overcounts by ~2.3x. Lines are
therefore grouped by message.id and the largest observed output_tokens wins --
early lines in a group carry partial counts from mid-stream.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

USAGE_MARKER = b'"usage"'
SYNTHETIC_MODEL = "<synthetic>"


@dataclass(slots=True)
class UsageRecord:
    message_id: str
    request_id: str | None
    source_id: str
    session_id: str | None
    project_path: str | None
    rel_path: str
    git_branch: str | None
    entrypoint: str | None
    model: str
    effort: str | None
    service_tier: str | None
    speed: str | None
    is_sidechain: int
    agent_id: str | None
    ts_utc: str
    local_date: str
    local_hour: int
    iso_week: str
    month: str
    year: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cache_read_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    web_search_requests: int
    web_fetch_requests: int


@dataclass(slots=True)
class ParseStats:
    lines_read: int = 0
    usage_lines: int = 0
    records: int = 0
    skipped_synthetic: int = 0
    skipped_no_timestamp: int = 0
    skipped_no_id: int = 0
    bad_json: int = 0
    collapsed: int = 0          # usage lines absorbed by dedup
    torn_tail: bool = False     # trailing line was incomplete and left unread


@dataclass(slots=True)
class ParseResult:
    records: list[UsageRecord] = field(default_factory=list)
    byte_offset: int = 0
    anchor_len: int = 0
    anchor_sha256: str | None = None
    anchor_uuid: str | None = None
    stats: ParseStats = field(default_factory=ParseStats)


def _derive_time(ts: str, tz) -> tuple[str, int, str, str, int]:
    """UTC timestamp string -> (local_date, local_hour, iso_week, month, year).

    The JSONL only ever carries UTC. Local time is computed, never read
    (PLAN.md D8). 2.5% of events land on a different calendar day once shifted.
    """
    utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    local = utc.astimezone(tz)
    iso = local.isocalendar()
    return (
        local.strftime("%Y-%m-%d"),
        local.hour,
        f"{iso[0]}-W{iso[1]:02d}",
        local.strftime("%Y-%m"),
        local.year,
    )


def parse_file(
    path: str,
    source_id: str,
    rel_path: str,
    tz,
    start_offset: int = 0,
) -> ParseResult:
    """Parse a JSONL file from `start_offset`, returning deduplicated records.

    Opened in binary: byte offsets and character offsets diverge on non-ASCII
    (CJK is 3 bytes, emoji 4), so a text-mode offset would eventually land
    mid-character. The offset advances only to the last newline-terminated
    line, leaving a torn trailing line for the next scan.
    """
    stats = ParseStats()
    best: dict[str, tuple[int, UsageRecord]] = {}
    consumed = start_offset
    last_line: bytes | None = None
    last_uuid: str | None = None

    with open(path, "rb") as fh:
        if start_offset:
            fh.seek(start_offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                stats.torn_tail = True
                break
            consumed += len(raw)
            last_line = raw
            stats.lines_read += 1

            if USAGE_MARKER not in raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                stats.bad_json += 1
                continue

            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue

            stats.usage_lines += 1
            last_uuid = obj.get("uuid") or last_uuid

            model = msg.get("model")
            if model == SYNTHETIC_MODEL or not model:
                stats.skipped_synthetic += 1
                continue

            message_id = msg.get("id") or obj.get("requestId")
            if not message_id:
                stats.skipped_no_id += 1
                continue

            ts = obj.get("timestamp")
            if not ts:
                stats.skipped_no_timestamp += 1
                continue
            try:
                local_date, local_hour, iso_week, month, year = _derive_time(ts, tz)
            except ValueError:
                stats.skipped_no_timestamp += 1
                continue

            out_tokens = int(usage.get("output_tokens") or 0)
            prev = best.get(message_id)
            if prev is not None:
                stats.collapsed += 1
                if prev[0] >= out_tokens:
                    continue

            cache_creation = usage.get("cache_creation") or {}
            details = usage.get("output_tokens_details") or {}
            server_tools = usage.get("server_tool_use") or {}

            best[message_id] = (
                out_tokens,
                UsageRecord(
                    message_id=message_id,
                    request_id=obj.get("requestId"),
                    source_id=source_id,
                    session_id=obj.get("sessionId"),
                    project_path=obj.get("cwd"),
                    rel_path=rel_path,
                    git_branch=obj.get("gitBranch"),
                    entrypoint=obj.get("entrypoint"),
                    model=model,
                    effort=obj.get("effort"),
                    service_tier=usage.get("service_tier"),
                    speed=usage.get("speed"),
                    is_sidechain=1 if obj.get("isSidechain") else 0,
                    agent_id=obj.get("agentId"),
                    ts_utc=ts,
                    local_date=local_date,
                    local_hour=local_hour,
                    iso_week=iso_week,
                    month=month,
                    year=year,
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=out_tokens,
                    thinking_tokens=int(details.get("thinking_tokens") or 0),
                    cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                    cache_write_5m_tokens=int(
                        cache_creation.get("ephemeral_5m_input_tokens") or 0
                    ),
                    cache_write_1h_tokens=int(
                        cache_creation.get("ephemeral_1h_input_tokens") or 0
                    ),
                    web_search_requests=int(server_tools.get("web_search_requests") or 0),
                    web_fetch_requests=int(server_tools.get("web_fetch_requests") or 0),
                ),
            )

    stats.records = len(best)
    return ParseResult(
        records=[rec for _, rec in best.values()],
        byte_offset=consumed,
        anchor_len=len(last_line) if last_line else 0,
        anchor_sha256=hashlib.sha256(last_line).hexdigest() if last_line else None,
        anchor_uuid=last_uuid,
        stats=stats,
    )


def is_mirror(rec: UsageRecord) -> bool:
    """True if this record is a remote-session mirror rather than the original.

    A Claude Code session run over SSH is written to the *client* machine under
    a project directory named `ssh-<sessionId>`, in addition to being written
    natively on the host that actually ran it. Both copies carry identical
    message ids and token counts, so the same API call appears under two
    sources -- and only the host's copy reflects the account that was billed.
    """
    return rec.rel_path.startswith("ssh-") or "/ssh-" in rec.rel_path


def _preference(rec: UsageRecord) -> tuple:
    """Ranking key for choosing between records of the same API call.

    Higher output_tokens wins first (a later stream snapshot is more complete).
    Mirrors lose to originals. source_id breaks remaining ties so the result
    does not depend on config ordering or filesystem iteration order.
    """
    return (rec.output_tokens, 0 if is_mirror(rec) else 1, rec.source_id)


def merge_records(batches, collisions: list | None = None):
    """Collapse records across files and sources, one row per API call.

    Dedup is global rather than per file: a session can be resumed, forked, or
    -- as with SSH sessions -- mirrored into a second source entirely.
    """
    best: dict[str, UsageRecord] = {}
    for rec in batches:
        prev = best.get(rec.message_id)
        if prev is None:
            best[rec.message_id] = rec
            continue
        if collisions is not None and prev.source_id != rec.source_id:
            collisions.append((rec.message_id, prev, rec))
        if _preference(rec) > _preference(prev):
            best[rec.message_id] = rec
    return best
