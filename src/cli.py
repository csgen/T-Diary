"""Command line entry point.

M0 provides `scan`, which exercises the sweep + parse pipeline and reports what
it found without writing anything. Storage lands in M1.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

from .config import ConfigError, load_config, resolve_account
from .parse import merge_records, parse_file
from .scan import SourceUnavailable, sweep


BYTES_PER_MB = 1024 * 1024      # mebibyte, matching how OS file managers report size


def _fmt_mb(n: int) -> str:
    """Bytes -> a human-readable MB string. os.scandir reports raw bytes."""
    return f"{n / BYTES_PER_MB:.1f} MB"


def cmd_scan(args) -> int:
    cfg = load_config(args.config)
    print(f"config: {cfg.root_dir}/config.toml   tz=UTC{cfg.timezone_offset_hours:+d}")

    grand_total: dict[str, object] = {}
    all_records = []
    failures = 0

    for source in cfg.sources:
        print(f"\n=== {source.id}  ({source.label}) ===")
        print(f"  path: {source.path}")

        account = resolve_account(source)
        if account.resolved:
            hint = source.account_hint
            flag = "" if not hint or hint == account.email else "  << DIFFERS FROM account_hint"
            print(f"  account: {account.email}  org={account.org_name}{flag}")
            print(f"           uuid={account.uuid}")
        else:
            print(f"  account: UNRESOLVED -- {account.error}")

        try:
            t0 = time.perf_counter()
            stats = sweep(source)
            sweep_ms = (time.perf_counter() - t0) * 1000
        except SourceUnavailable as exc:
            print(f"  SKIPPED: {exc}", file=sys.stderr)
            failures += 1
            continue

        total_bytes = sum(s.size for s in stats)
        hot = [s for s in stats if s.age_hours() < cfg.hot_window_hours]
        print(
            f"  sweep: {len(stats)} files, {_fmt_mb(total_bytes)} in {sweep_ms:.0f} ms"
            f"   (hot <{cfg.hot_window_hours}h: {len(hot)} files, {_fmt_mb(sum(s.size for s in hot))})"
        )

        if args.sweep_only:
            continue

        t0 = time.perf_counter()
        records, lines, usage_lines, collapsed, torn = [], 0, 0, 0, 0
        for st in stats:
            res = parse_file(st.path, source.id, st.rel_path, cfg.tz)
            records.extend(res.records)
            lines += res.stats.lines_read
            usage_lines += res.stats.usage_lines
            collapsed += res.stats.collapsed
            torn += 1 if res.stats.torn_tail else 0
        parse_ms = (time.perf_counter() - t0) * 1000

        merged = merge_records(records)
        all_records.extend(merged.values())
        dates = sorted({r.local_date for r in merged.values()})
        models = Counter(r.model for r in merged.values())
        sidechain = sum(1 for r in merged.values() if r.is_sidechain)

        print(f"  parse: {parse_ms:.0f} ms")
        print(f"    raw lines        {lines:>9,}")
        print(f"    usage lines      {usage_lines:>9,}")
        print(f"    unique API calls {len(merged):>9,}   (collapsed {collapsed:,})")
        print(f"    active days      {len(dates):>9,}   {dates[0]} .. {dates[-1]}" if dates else "    active days: 0")
        print(f"    sidechain calls  {sidechain:>9,}")
        print(f"    torn tails       {torn:>9,}")
        for model, n in models.most_common():
            print(f"      {model:<32} {n:>7,}")
        grand_total[source.id] = len(merged)

    if not args.sweep_only and all_records:
        collisions: list = []
        combined = merge_records(all_records, collisions=collisions)
        tokens = sum(
            r.input_tokens + r.output_tokens + r.cache_read_tokens
            + r.cache_write_5m_tokens + r.cache_write_1h_tokens
            for r in combined.values()
        )
        print("\n=== combined ===")
        for sid, n in grand_total.items():
            print(f"  {sid:<10} {n:>8,} calls")
        print(f"  {'TOTAL':<10} {len(combined):>8,} calls   {tokens:,} tokens")
        if collisions:
            won = Counter(combined[mid].source_id for mid, _, _ in collisions)
            print(
                f"\n  {len(collisions)} API call(s) recorded in more than one source"
                " (SSH session mirrors); counted once, attributed to:"
            )
            for sid, n in won.most_common():
                print(f"    {sid:<10} {n:>4}")

    if failures:
        print(f"\n{failures} source(s) unavailable", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tokendiary", description="Claude Code usage tracker")
    p.add_argument("-c", "--config", help="path to config.toml")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="sweep and parse sources; write nothing")
    s.add_argument("--sweep-only", action="store_true", help="stat sweep only, do not parse")
    s.set_defaults(func=cmd_scan)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
