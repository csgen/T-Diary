"""Command line entry point.

M0 provides `scan`, which exercises the sweep + parse pipeline and reports what
it found without writing anything. Storage lands in M1.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from collections import Counter

from .config import ConfigError, load_config, resolve_account
from .parse import merge_records, parse_file
from .scan import SourceUnavailable, detect_deleted, sweep
from .store import Store


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


def cmd_ingest(args) -> int:
    """Scan every source and persist what is found (PLAN.md M1).

    M1 parses each file in full; offsets and the hot-file rule arrive in M3.
    That is deliberate -- it keeps the acceptance check honest, since a second
    run re-reads everything and must still change nothing.
    """
    cfg = load_config(args.config)
    targets = [s for s in cfg.sources if not args.source or s.id in args.source]
    if not targets:
        print(f"no source matches {args.source}", file=sys.stderr)
        return 2

    with Store(cfg.db_path) as store:
        store.init_schema()
        started = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        batch_id = store.begin_batch(
            "full" if args.full else "incremental",
            json.dumps([s.id for s in targets]),
        )
        print(f"batch {batch_id}  db={cfg.db_path}")

        all_records, ok_sources, failures = [], [], 0

        for source in targets:
            account = resolve_account(source)
            store.upsert_source(source, account)
            if not account.resolved:
                print(f"  {source.id}: account unresolved -- {account.error}", file=sys.stderr)

            try:
                files = sweep(source)
            except SourceUnavailable as exc:
                # Never commit an empty scan: a silent zero renders as "you did
                # no work that day", which is worse than a visible gap (§6.6).
                print(f"  {source.id}: SKIPPED -- {exc}", file=sys.stderr)
                failures += 1
                continue

            t0 = time.perf_counter()
            records, torn = [], 0
            for st in files:
                res = parse_file(st.path, source.id, st.rel_path, cfg.tz)
                records.extend(res.records)
                torn += 1 if res.stats.torn_tail else 0
                store.record_scan(
                    source.id, st.path, st.size, st.mtime, res.byte_offset,
                    res.anchor_len, res.anchor_sha256, res.anchor_uuid, "full",
                )
            elapsed = (time.perf_counter() - t0) * 1000

            state = store.load_scan_state(source.id)
            pruned = detect_deleted(files, state)
            if pruned:
                store.mark_deleted(source.id, pruned)

            all_records.extend(records)
            ok_sources.append((source, started))
            print(
                f"  {source.id:<10} {len(files):>3} files  {len(records):>6,} calls"
                f"  {elapsed:>7.0f} ms"
                + (f"  pruned:{len(pruned)}" if pruned else "")
                + (f"  torn:{torn}" if torn else "")
            )

        collisions: list = []
        merged = merge_records(all_records, collisions=collisions)

        if args.dry_run:
            existing = store.existing_ids(merged)
            print(f"\ndry run: {len(merged):,} calls parsed, "
                  f"{len(merged) - len(existing):,} would be new; nothing written")
            store.con.rollback()
            return 1 if failures else 0

        result = store.upsert_events(merged.values(), batch_id)
        for source, ts in ok_sources:
            store.set_watermark(source.id, ts, full=args.full)
        store.finish_batch(batch_id, result, notes=f"{failures} source(s) unavailable"
                           if failures else "")
        store.con.commit()

        print(f"\n  inserted {result.inserted:,}   revised {result.revised:,}"
              f"   unchanged {result.unchanged:,}   (of {result.seen:,} seen)")
        if collisions:
            print(f"  {len(collisions)} cross-source duplicate(s) counted once")

    if failures:
        print(f"{failures} source(s) unavailable", file=sys.stderr)
        return 1
    return 0


def cmd_stats(args) -> int:
    cfg = load_config(args.config)
    if not os.path.exists(cfg.db_path):
        print(f"no database yet at {cfg.db_path} -- run `ingest` first", file=sys.stderr)
        return 2
    with Store(cfg.db_path) as store:
        s = store.stats()
        print(f"database   {cfg.db_path}  ({_fmt_mb(s['db_bytes'])}, schema v{store.schema_version()})")
        print(f"rows       {s['rows']:,} API calls   {s['tokens']:,} tokens")
        print(f"coverage   {s['first_date']} .. {s['last_date']}   {s['active_days']} active days")
        print(f"batches    {s['batches']}   revised rows {s['revised_rows']}"
              f"   pruned files {s['pruned_files']}")
        print()
        for row in s["by_source"]:
            print(f"  {row['source_id']:<10} {row['rows']:>7,} calls   "
                  f"{row['d0'] or '-'} .. {row['d1'] or '-'}   {row['account_email'] or ''}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tokendiary", description="Claude Code usage tracker")
    p.add_argument("-c", "--config", help="path to config.toml")
    sub = p.add_subparsers(dest="command", required=True)

    sc = sub.add_parser("scan", help="sweep and parse sources; write nothing")
    sc.add_argument("--sweep-only", action="store_true", help="stat sweep only, do not parse")
    sc.set_defaults(func=cmd_scan)

    ing = sub.add_parser("ingest", help="scan sources and persist to the database")
    ing.add_argument("--full", action="store_true", help="ignore scan state; reparse everything")
    ing.add_argument("--source", action="append", help="limit to a source id (repeatable)")
    ing.add_argument("--dry-run", action="store_true", help="parse and report; write nothing")
    ing.set_defaults(func=cmd_ingest)

    st = sub.add_parser("stats", help="summarize what is stored")
    st.set_defaults(func=cmd_stats)
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
