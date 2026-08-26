"""Stage 1: the stat sweep (PLAN.md §6.1).

Decides which files are worth opening, using only directory-entry metadata.
Measured at 58 ms for 76 files with zero file opens, versus ~6 s to read all
135 MB. Nothing here is load-bearing for correctness (PLAN.md D4): every fast
path fails safe by falling back to a full reparse.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

from .config import Source, norm_path

HEAD_BYTES = 4096 # read 4kb from the head to decide whether the file still the same (not get compacted)


class SourceUnavailable(Exception):
    """A source root is missing or yielded no files.

    Raised rather than returning empty, because committing an empty scan would
    render as "you did no work that day" -- worse than a visible gap
    (PLAN.md §6.6).
    """


@dataclass(slots=True)
class FileStat:
    """One .jsonl file as the sweep sees it -- directory-entry metadata only.

    Everything here comes from os.scandir without opening the file. These five
    facts are all the sweep needs to decide whether the file is worth reading.
    """

    source_id: str      # which configured source it belongs to, e.g. 'laptop'
    path: str           # absolute, forward-slashed
    rel_path: str       # path relative to the source root; what dim_file stores
    size: int           # bytes
    mtime: float        # unix epoch seconds, from the filesystem

    def age_hours(self, now: float | None = None) -> float:
        """Hours since last modification. Drives the hot-file rule (D5)."""
        return ((now if now is not None else time.time()) - self.mtime) / 3600.0


@dataclass(slots=True)
class Candidate:
    """A swept file that needs reading, plus why.

    A FileStat is what we observed; a Candidate is what we decided about it.
    """

    file: FileStat
    reason: str      # why it was selected: 'new' | 'mtime' | 'size' | 'hot' | 'full'
    hot: bool        # modified within hot_window_hours -> must parse from byte 0 (D5)


def sweep(source: Source) -> list[FileStat]:
    """Enumerate every .jsonl under a source root via os.scandir."""
    root = norm_path(source.path)
    if not os.path.isdir(root):
        raise SourceUnavailable(f"source {source.id!r}: root not reachable: {root}")

    out: list[FileStat] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise SourceUnavailable(
                f"source {source.id!r}: cannot read {current} ({exc.__class__.__name__})"
            ) from exc
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(norm_path(e.path))
                elif e.name.endswith(".jsonl"):
                    st = e.stat()
                    p = norm_path(e.path)
                    out.append(
                        FileStat(
                            source_id=source.id,
                            path=p,
                            rel_path=source.rel(p),
                            size=st.st_size,
                            mtime=st.st_mtime,
                        )
                    )
            except OSError:
                continue      # entry vanished mid-sweep; the next run will catch it

    if not out:
        raise SourceUnavailable(f"source {source.id!r}: no .jsonl files under {root}")
    return sorted(out, key=lambda f: f.path)


def select_candidates(
    stats: list[FileStat],
    state: dict[str, dict],
    watermark: float | None, # last successful scan time
    slack_seconds: int,
    hot_window_hours: int,
    now: float | None = None,
    force_full: bool = False,
) -> list[Candidate]:
    """Decide which swept files need reading.

    `state` maps file_path -> stored scan_state row (empty until M1/M3).
    A file is a candidate if it is new, its mtime passed the watermark, or its
    size disagrees with what we recorded. Size is OR-ed in because mtime can
    lie -- restored backups and copies that preserve timestamps.

    `slack_seconds` and `hot_window_hours` are required rather than defaulted:
    they are tuning values owned by config.toml, so a default here would be a
    second home for them. Pass cfg.watermark_slack_seconds and
    cfg.hot_window_hours. Fallbacks for a missing config key live in
    config.DEFAULT_* and nowhere else.
    """
    now = time.time() if now is None else now
    cutoff = None if watermark is None else watermark - slack_seconds
    out: list[Candidate] = []

    for st in stats:
        hot = st.age_hours(now) < hot_window_hours
        prev = state.get(st.path)
        if force_full:
            reason = "full"
        elif prev is None:
            reason = "new"
        elif st.size != prev.get("size"):
            reason = "size"
        elif cutoff is None or st.mtime > cutoff:
            reason = "mtime"
        elif hot:
            reason = "hot"
        else:
            continue
        out.append(Candidate(file=st, reason=reason, hot=hot))
    return out


def detect_deleted(stats: list[FileStat], state: dict[str, dict]) -> list[str]:
    """Files present in scan_state but absent from disk -- i.e. Claude pruned them.

    Free byproduct of the sweep. Rows are never removed (PLAN.md D2); this only
    stamps scan_state.deleted_at so pruning events become observable.
    """
    seen = {s.path for s in stats}
    return [p for p, row in state.items() if p not in seen and not row.get("deleted_at")]


def head_sha256(path: str, nbytes: int = HEAD_BYTES) -> str:
    """Hash of the first `nbytes`, for detecting a rewritten prefix (PLAN.md §6.2)."""
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read(nbytes)).hexdigest()


def read_anchor(path: str, byte_offset: int, anchor_len: int) -> bytes | None:
    """Read the last line consumed, for verifying a stored offset is still valid."""
    if anchor_len <= 0 or byte_offset < anchor_len:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(byte_offset - anchor_len)
            return fh.read(anchor_len)
    except OSError:
        return None
