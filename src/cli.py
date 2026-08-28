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
from .price import PriceError, PriceRevConflict, compute_cost, load_prices
from .scan import (SourceUnavailable, detect_deleted, head_bytes, head_sha256,
                   plan_read, select_candidates, sweep)
from .store import SCHEMA_VERSION, SchemaTooNew, Store


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
        for applied in store.init_schema():
            print(f"  schema migration applied: {applied}")
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

            # Stage 1: what changed since the last successful scan.
            state = store.load_scan_state(source.id)
            candidates = select_candidates(
                files, state, store.watermark(source.id),
                slack_seconds=cfg.watermark_slack_seconds,
                hot_window_hours=cfg.hot_window_hours,
                force_full=args.full,
            )

            t0 = time.perf_counter()
            records, torn, bytes_read = [], 0, 0
            modes = Counter()
            for cand in candidates:
                st = cand.file
                plan = plan_read(cand, state.get(st.path))
                modes[plan.reason] += 1
                if plan.mode == "skip":
                    continue

                res = parse_file(st.path, source.id, st.rel_path, cfg.tz,
                                 start_offset=plan.start_offset)
                records.extend(res.records)
                torn += 1 if res.stats.torn_tail else 0
                bytes_read += res.byte_offset - plan.start_offset

                # A resume that found no complete new line must not erase the
                # anchor that made the resume possible.
                prev = state.get(st.path) or {}
                anchor_len = res.anchor_len or (prev.get("anchor_len") or 0)
                anchor_sha = res.anchor_sha256 or prev.get("anchor_sha256")
                anchor_uuid = res.anchor_uuid or prev.get("anchor_uuid")

                store.record_scan(
                    source.id, st.path, st.size, st.mtime, res.byte_offset,
                    anchor_len, anchor_sha, anchor_uuid, plan.reason,
                    head_sha256=head_sha256(st.path, head_bytes(res.byte_offset)),
                    was_reset=plan.is_reset,
                )
            elapsed = (time.perf_counter() - t0) * 1000

            pruned = detect_deleted(files, state)
            if pruned:
                store.mark_deleted(source.id, pruned)

            all_records.extend(records)
            ok_sources.append((source, started))
            skipped = len(files) - len(candidates)
            detail = " ".join(f"{k}:{v}" for k, v in sorted(modes.items()) if k != "unchanged")
            print(
                f"  {source.id:<10} {len(files):>3} files"
                f"  read {_fmt_mb(bytes_read):>9}  {len(records):>6,} calls"
                f"  {elapsed:>7.0f} ms"
                + (f"  skipped:{skipped}" if skipped else "")
                + (f"  pruned:{len(pruned)}" if pruned else "")
                + (f"  torn:{torn}" if torn else "")
                + (f"   [{detail}]" if detail else "")
            )

        collisions: list = []
        merged = merge_records(all_records, collisions=collisions)

        if cfg.ingest_from:
            before = len(merged)
            merged = {k: r for k, r in merged.items()
                      if r.local_date >= cfg.ingest_from}
            if before != len(merged):
                print(f"  ingest_from={cfg.ingest_from}: skipped "
                      f"{before - len(merged):,} call(s) before that date")

        if args.dry_run:
            existing = store.existing_ids(merged)
            print(f"\ndry run: {len(merged):,} calls parsed, "
                  f"{len(merged) - len(existing):,} would be new; nothing written")
            store.con.rollback()
            return 1 if failures else 0

        result = store.upsert_events(merged.values(), batch_id)

        # Cost is computed once, here, and frozen into the row.
        # Editing prices.json later does not disturb it; only `recost` does.
        try:
            prices = load_prices(cfg.root_dir)
            rev, created = store.register_price_rev(prices)
            if created:
                print(f"  recorded price revision {rev} (declared in prices.json)")
            priced = unpriced = 0
            for row in store.uncosted():
                rates = prices.resolve(row["model"], row["local_date"], row["speed"])
                if rates is None:
                    store.set_cost(row["message_id"], None, None, None)
                    unpriced += 1
                    continue
                cost, parts = compute_cost(row, rates, prices)
                store.set_cost(row["message_id"], cost, json.dumps(parts), rev)
                priced += 1
            if priced or unpriced:
                print(f"  priced {priced:,} new row(s) at rev {rev}"
                      + (f"; {unpriced:,} unpriced (unknown model)" if unpriced else ""))
            # Existing rows keep the cost they were given. Report the drift so
            # bringing them forward stays a deliberate `recost`, never a
            # side effect of a routine scan.
            stale = store.rows_on_other_revs(rev)
            if stale:
                detail = ", ".join(f"{s['n']:,} at rev {s['price_rev']}" for s in stale)
                print(f"  note: {detail}; current table is rev {rev}. "
                      f"Run `recost --dry-run` to see what would change.")
        except PriceRevConflict:
            raise                      # a declared revision that lies is fatal
        except PriceError as exc:
            print(f"  pricing skipped: {exc}", file=sys.stderr)
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


def cmd_recost(args) -> int:
    """Re-price stored rows against the current prices.json.

    History is never rewritten implicitly. This is the only path that changes a
    stored cost, it reports the delta before touching anything, and the
    superseded revision's snapshot stays in price_rev for audit.
    """
    cfg = load_config(args.config)
    if not os.path.exists(cfg.db_path):
        print(f"no database at {cfg.db_path}", file=sys.stderr)
        return 2

    with Store(cfg.db_path) as store:
        prices = load_prices(cfg.root_dir)
        # A dry run must write nothing at all -- including a price revision.
        # Registering one here would leave an audit row for a table that never
        # priced anything, which is exactly the kind of noise the revision log
        # exists to avoid.
        if args.dry_run:
            rev, is_new = store.check_price_rev(prices)          # validates, writes nothing
            print(f"prices.json declares rev {rev}"
                  + (" (not yet recorded; a real run would record it)" if is_new else ""))
        else:
            rev, is_new = store.register_price_rev(prices)
            print(f"recorded price revision {rev}" if is_new
                  else f"prices.json declares rev {rev}, already recorded")

        rows = store.priced_between(args.from_date, args.model)
        if not rows:
            print("no rows in range")
            return 0

        changes, unknown, delta_by_month = [], 0, {}
        for row in rows:
            rates = prices.resolve(row["model"], row["local_date"], row["speed"])
            if rates is None:
                unknown += 1
                continue
            new_cost, parts = compute_cost(row, rates, prices)
            old_cost = row["cost_usd"]
            if old_cost is None or abs(new_cost - old_cost) > 1e-12 or row["price_rev"] != rev:
                changes.append((row["message_id"], new_cost, json.dumps(parts)))
                d = delta_by_month.setdefault(row["month"], [0.0, 0.0, 0])
                d[0] += old_cost or 0.0
                d[1] += new_cost
                d[2] += 1

        print(f"\n{len(rows):,} row(s) in range; {len(changes):,} would change"
              + (f"; {unknown:,} unpriced (unknown model)" if unknown else ""))
        if delta_by_month:
            print(f"\n  {'month':<10} {'rows':>7} {'old':>12} {'new':>12} {'delta':>12}")
            for month in sorted(delta_by_month):
                old, new, n = delta_by_month[month]
                print(f"  {month:<10} {n:>7,} {old:>12.4f} {new:>12.4f} {new - old:>+12.4f}")
            t_old = sum(v[0] for v in delta_by_month.values())
            t_new = sum(v[1] for v in delta_by_month.values())
            print(f"  {'TOTAL':<10} {len(changes):>7,} {t_old:>12.4f} {t_new:>12.4f} {t_new - t_old:>+12.4f}")

        if args.dry_run:
            print("\ndry run: nothing written")
            return 0
        if not changes:
            print("\nnothing to do")
            return 0

        for message_id, cost, parts in changes:
            store.set_cost(message_id, cost, parts, rev)
        store.con.commit()
        print(f"\nupdated {len(changes):,} row(s) to revision {rev}")
    return 0


def cmd_migrate(args) -> int:
    """Inspect or apply pending schema migrations.

    `ingest` migrates automatically, so this exists for the case that matters:
    looking at what a migration will do to a database holding the only surviving
    copy of pruned history, before it happens.
    """
    cfg = load_config(args.config)
    if not os.path.exists(cfg.db_path):
        print(f"no database at {cfg.db_path} -- `ingest` creates one", file=sys.stderr)
        return 2

    with Store(cfg.db_path) as store:
        found = store.stored_version()
        print(f"database   {cfg.db_path}")
        print(f"schema     v{found} stored, v{SCHEMA_VERSION} supported by this build")

        if found > SCHEMA_VERSION:
            print()
            print("refusing: database is NEWER than this build understands.",
                  file=sys.stderr)
            return 1

        pending = store.pending_migrations()
        print()
        if not pending:
            print("up to date; nothing to apply")
            return 0

        print(f"{len(pending)} pending:")
        for item in pending:
            print(f"  {item}")
        print()

        if args.check:
            print("--check: nothing written")
            return 0

        applied = store.migrate()
        print(f"applied {len(applied)}; database is now v{store.schema_version()}")
        print("note: new columns are added empty -- existing rows are not backfilled")
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

    rc = sub.add_parser("recost", help="re-price stored rows against current prices.json")
    rc.add_argument("--from", dest="from_date", metavar="DATE",
                    help="only rows on or after this local date (YYYY-MM-DD)")
    rc.add_argument("--model", help="limit to one model id")
    rc.add_argument("--dry-run", action="store_true",
                    help="report the delta without writing")
    rc.set_defaults(func=cmd_recost)

    mg = sub.add_parser("migrate", help="inspect or apply pending schema migrations")
    mg.add_argument("--check", action="store_true",
                    help="report what is pending without applying it")
    mg.set_defaults(func=cmd_migrate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except SchemaTooNew as exc:
        print(f"schema error: {exc}", file=sys.stderr)
        return 3
    except PriceError as exc:
        print(f"price error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
