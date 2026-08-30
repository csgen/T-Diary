"""SQLite -> web/data.json, the file the static dashboard reads.

The dashboard is a plain HTML page opened over file://, so it cannot query
SQLite. This step hands it everything pre-aggregated.

Grain is date x source x model x sidechain -- fine enough for every filter the
UI offers, coarse enough that a year is a few hundred KB. Anything the page can
compute by summing, it computes; only what SQL does better is precomputed.

Audit columns are deliberately never read here (PLAN.md invariant 6): if export
depended on `updated_at`, the dashboard would change with WHEN a scan ran.
"""

from __future__ import annotations

import datetime
import json
import os

# Short keys: at daily x source x model x sidechain grain this file is mostly
# repeated field names, and the page is the only reader.
DAILY_SQL = """
SELECT e.local_date          AS d,
       e.source_id           AS s,
       e.model               AS m,
       e.is_sidechain        AS x,
       COUNT(*)              AS n,
       SUM(e.input_tokens)          AS i,
       SUM(e.output_tokens)         AS o,
       SUM(e.thinking_tokens)       AS th,
       SUM(e.cache_read_tokens)     AS cr,
       SUM(e.cache_write_5m_tokens) AS c5,
       SUM(e.cache_write_1h_tokens) AS c1,
       SUM(COALESCE(e.cost_usd, 0)) AS cost,
       SUM(e.cost_usd IS NULL)      AS unpriced
  FROM usage_event e
 GROUP BY e.local_date, e.source_id, e.model, e.is_sidechain
 ORDER BY e.local_date
"""

HOURLY_SQL = """
SELECT local_hour AS h, COUNT(*) AS n,
       SUM(COALESCE(cost_usd, 0)) AS cost
  FROM usage_event GROUP BY local_hour ORDER BY local_hour
"""


def build(store, cfg) -> dict:
    """Assemble the payload. Pure read -- never writes to the database."""
    q = store.con.execute

    daily = [dict(r) for r in q(DAILY_SQL)]
    hourly = [dict(r) for r in q(HOURLY_SQL)]

    sources = [dict(r) for r in q(
        """SELECT s.source_id AS id, s.label, s.account_email AS email,
                  s.org_name AS org, s.last_seen,
                  COUNT(e.message_id) AS rows_
             FROM dim_source s LEFT JOIN usage_event e USING(source_id)
            GROUP BY s.source_id ORDER BY s.source_id""")]

    cover = q("""SELECT MIN(local_date) f, MAX(local_date) l,
                        COUNT(DISTINCT local_date) days, COUNT(*) n
                   FROM usage_event""").fetchone()

    price_rev = q("SELECT MAX(price_rev) r FROM usage_event").fetchone()["r"]
    pruned = q("SELECT COUNT(*) n FROM scan_state "
               "WHERE deleted_at IS NOT NULL").fetchone()["n"]
    last_batch = q("SELECT MAX(finished_at) t FROM ingest_batch").fetchone()["t"]

    # "Today" in the user's own timezone, so the dashboard can mark the current
    # day provisional -- it can still tick upward until sessions close (D3).
    now_local = datetime.datetime.now(cfg.tz)

    return {
        "meta": {
            "generated_at": datetime.datetime.now(datetime.timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "today": now_local.strftime("%Y-%m-%d"),
            "tz_label": f"UTC{cfg.timezone_offset_hours:+d}",
            "week_start": cfg.week_start,
            "default_from": cfg.dashboard_default_from,
            "price_rev": price_rev,
            "pruned_files": pruned,
            "last_ingest": last_batch,
            "coverage": {"first": cover["f"], "last": cover["l"],
                         "active_days": cover["days"], "calls": cover["n"]},
            "sources": sources,
            "models": sorted({r["m"] for r in daily}),
            "cost_note": "Notional: what this usage would cost at API list rates.",
        },
        "daily": daily,
        "hourly": hourly,
    }


def write(payload: dict, out_path: str) -> int:
    """Write the payload, returning its size in bytes."""
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
        fh.write("\n")
    os.replace(tmp, out_path)
    return os.path.getsize(out_path)
