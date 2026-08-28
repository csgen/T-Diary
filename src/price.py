"""Cost computation and rate-table versioning.

Two independent time axes:

  effective_from   when a price applied to USAGE      -- inside prices.json
  rev              when we RECORDED that price        -- the price_rev table

Editing prices.json never rewrites history. Cost is computed once at ingest and
frozen into the row; only an explicit `recost` changes it, and the superseded
revision's full snapshot is retained so "why did June change?" stays answerable.

Cost here is NOTIONAL -- what the usage would have cost at API list rates. Both
tracked accounts are subscription-billed, so it is an intensity metric.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

PRICES_NAME = "prices.json"
PER_MILLION = 1_000_000

# Model ids in the JSONL are pinned snapshots and sometimes carry a date suffix
# (claude-haiku-4-5-20251001), while the price table is keyed by the base id.
_DATE_SUFFIX = re.compile(r"-\d{8}$")


class PriceError(Exception):
    """prices.json is missing or malformed."""


class PriceRevConflict(PriceError):
    """The rates changed but `rev` was not bumped.

    Revisions are declared by hand, not inferred from the file, so nothing
    happens when comments are reworded or the file is reformatted. The one
    thing that must not happen is two different rate tables sharing a revision
    number: the stored costs would then be unauditable, because the snapshot
    behind them no longer matches what produced them.
    """


def semantic_hash(data: dict) -> str:
    """Fingerprint of the parts of prices.json that can change a cost.

    NOT used to decide the revision number -- that is declared by hand in the
    file's `rev` field. This exists only as an integrity check: it catches a
    rate edited without bumping `rev`, which would leave stored costs pointing
    at a snapshot that no longer explains them.

    Only `models` and `server_tools` are covered, canonically serialized, so
    rewording notes or reformatting the file changes nothing.
    """
    payload = {
        "models": data.get("models") or {},
        "server_tools": {k: v for k, v in (data.get("server_tools") or {}).items()
                         if not k.startswith("_")},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class RateCard:
    """The five per-MTok rates that apply to one call."""

    model: str
    effective_from: str
    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    speed: str = "standard"


@dataclass(slots=True)
class Prices:
    rev: int
    models: dict[str, list[dict]]
    web_search_per_request: float
    web_fetch_per_request: float
    content_hash: str
    raw: str
    path: str

    def normalize(self, model: str) -> str:
        """Map a pinned snapshot id onto its price-table key."""
        if model in self.models:
            return model
        stripped = _DATE_SUFFIX.sub("", model)
        return stripped if stripped in self.models else model

    def resolve(self, model: str, on_date: str, speed: str | None = None) -> RateCard | None:
        """The rate card in force for `model` on `on_date`.

        Returns None for an unknown model rather than guessing -- an unpriced row
        is stored with cost NULL and surfaced by `verify`, never silently 0.
        """
        entries = self.models.get(self.normalize(model))
        if not entries:
            return None
        applicable = [e for e in entries if e.get("effective_from", "") <= on_date]
        if not applicable:
            return None
        entry = max(applicable, key=lambda e: e.get("effective_from", ""))

        # Fast mode is a premium tier on some models; caching multipliers apply
        # on top of it, so the whole card is swapped rather than scaled.
        if speed and speed != "standard" and isinstance(entry.get(speed), dict):
            card = {**entry, **entry[speed]}
        else:
            card = entry

        return RateCard(
            model=self.normalize(model),
            effective_from=entry.get("effective_from", ""),
            input=float(card["input"]),
            output=float(card["output"]),
            cache_write_5m=float(card["cache_write_5m"]),
            cache_write_1h=float(card["cache_write_1h"]),
            cache_read=float(card["cache_read"]),
            speed=speed or "standard",
        )


def find_prices(root_dir: str) -> str:
    return f"{root_dir}/{PRICES_NAME}"


def load_prices(root_dir: str) -> Prices:
    path = find_prices(root_dir)
    if not os.path.exists(path):
        raise PriceError(f"no {PRICES_NAME} found at {path}")
    with open(path, "rb") as fh:
        raw_bytes = fh.read()
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except ValueError as exc:
        raise PriceError(f"{path}: invalid JSON ({exc})") from exc

    rev = data.get("rev")
    if not isinstance(rev, int) or rev < 1:
        raise PriceError(
            f'{path}: needs a top-level integer "rev" (1 or greater). '
            f"Revisions are declared, not inferred: bump it yourself when you "
            f"change a rate, and leave it alone for comment or formatting edits."
        )

    models = data.get("models") or {}
    if not models:
        raise PriceError(f"{path}: no 'models' section")
    for name, entries in models.items():
        if not isinstance(entries, list) or not entries:
            raise PriceError(f"{path}: model {name!r} has no rate entries")
        for e in entries:
            for key in ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read"):
                if key not in e:
                    raise PriceError(f"{path}: model {name!r} entry is missing {key!r}")

    tools = data.get("server_tools") or {}
    return Prices(
        rev=rev,
        models=models,
        web_search_per_request=float(tools.get("web_search_per_request", 0.0)),
        web_fetch_per_request=float(tools.get("web_fetch_per_request", 0.0)),
        # Integrity check only; the revision number comes from `rev` above.
        content_hash=semantic_hash(data),
        raw=raw_bytes.decode("utf-8"),
        path=path,
    )


def compute_cost(row, rates: RateCard, prices: Prices) -> tuple[float, dict]:
    """Cost in USD for one API call, plus a per-component breakdown.

    `row` is anything with the token attributes -- a parsed UsageRecord or a
    sqlite3.Row. thinking_tokens are deliberately NOT added: they are already a
    subset of output_tokens, and counting them again would inflate every
    thinking-heavy call.
    """
    def get(name: str) -> int:
        if isinstance(row, dict) or hasattr(row, "keys"):
            return int(row[name] or 0)
        return int(getattr(row, name, 0) or 0)

    parts = {
        "input": get("input_tokens") / PER_MILLION * rates.input,
        "output": get("output_tokens") / PER_MILLION * rates.output,
        "cache_read": get("cache_read_tokens") / PER_MILLION * rates.cache_read,
        "cache_write_5m": get("cache_write_5m_tokens") / PER_MILLION * rates.cache_write_5m,
        "cache_write_1h": get("cache_write_1h_tokens") / PER_MILLION * rates.cache_write_1h,
        "web_search": get("web_search_requests") * prices.web_search_per_request,
        "web_fetch": get("web_fetch_requests") * prices.web_fetch_per_request,
    }
    total = sum(parts.values())
    parts["_model"] = rates.model
    parts["_speed"] = rates.speed
    parts["_effective_from"] = rates.effective_from
    return total, parts
