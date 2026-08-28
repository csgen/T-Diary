"""Tests for the M2 pricing layer.

The properties that matter: a stored cost never changes on its own, an unknown
model produces NULL rather than a plausible-looking zero, and the published
cache multipliers are applied to the right token buckets.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.price import PriceError, compute_cost, load_prices, semantic_hash  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Row(dict):
    """Stands in for a sqlite3.Row or a UsageRecord."""


def row(**kw):
    base = dict(input_tokens=0, output_tokens=0, thinking_tokens=0,
                cache_read_tokens=0, cache_write_5m_tokens=0,
                cache_write_1h_tokens=0, web_search_requests=0,
                web_fetch_requests=0)
    base.update(kw)
    return Row(base)


def write_prices(models: dict, tools: dict | None = None, rev: int = 1,
                 meta: dict | None = None) -> str:
    d = tempfile.mkdtemp()
    payload = {"rev": rev, "models": models,
               "server_tools": tools or {"web_search_per_request": 0.01}}
    if meta is not None:
        payload["meta"] = meta
    with open(f"{d}/prices.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return d


SIMPLE = {
    "m1": [{"effective_from": "2000-01-01", "input": 10.0, "output": 100.0,
            "cache_write_5m": 12.5, "cache_write_1h": 20.0, "cache_read": 1.0}]
}


class ResolveTests(unittest.TestCase):
    def test_rate_lookup(self):
        p = load_prices(write_prices(SIMPLE))
        r = p.resolve("m1", "2026-06-01")
        self.assertEqual((r.input, r.output, r.cache_read), (10.0, 100.0, 1.0))

    def test_unknown_model_returns_none_not_zero(self):
        """An unpriced row must be visibly NULL, never a plausible $0.00."""
        p = load_prices(write_prices(SIMPLE))
        self.assertIsNone(p.resolve("no-such-model", "2026-06-01"))

    def test_dated_snapshot_id_falls_back_to_base_id(self):
        """Real ids carry date suffixes: claude-haiku-4-5-20251001."""
        p = load_prices(write_prices({"claude-haiku-4-5": SIMPLE["m1"]}))
        self.assertIsNotNone(p.resolve("claude-haiku-4-5-20251001", "2026-06-01"))

    def test_latest_entry_at_or_before_the_date_wins(self):
        models = {"m1": [
            {"effective_from": "2000-01-01", "input": 10.0, "output": 100.0,
             "cache_write_5m": 12.5, "cache_write_1h": 20.0, "cache_read": 1.0},
            {"effective_from": "2026-07-01", "input": 8.0, "output": 80.0,
             "cache_write_5m": 10.0, "cache_write_1h": 16.0, "cache_read": 0.8},
        ]}
        p = load_prices(write_prices(models))
        self.assertEqual(p.resolve("m1", "2026-06-30").output, 100.0)
        self.assertEqual(p.resolve("m1", "2026-07-01").output, 80.0)
        self.assertEqual(p.resolve("m1", "2026-12-31").output, 80.0)

    def test_date_before_every_entry_is_unpriced(self):
        models = {"m1": [{"effective_from": "2026-07-01", "input": 1.0, "output": 1.0,
                          "cache_write_5m": 1.0, "cache_write_1h": 1.0, "cache_read": 1.0}]}
        p = load_prices(write_prices(models))
        self.assertIsNone(p.resolve("m1", "2026-06-30"))

    def test_fast_mode_swaps_the_whole_card(self):
        """Cache multipliers apply on top of fast-mode pricing, not beside it."""
        models = {"m1": [{**SIMPLE["m1"][0],
                          "fast": {"input": 20.0, "output": 200.0, "cache_write_5m": 25.0,
                                   "cache_write_1h": 40.0, "cache_read": 2.0}}]}
        p = load_prices(write_prices(models))
        self.assertEqual(p.resolve("m1", "2026-06-01", "standard").output, 100.0)
        self.assertEqual(p.resolve("m1", "2026-06-01", "fast").output, 200.0)
        self.assertEqual(p.resolve("m1", "2026-06-01", "fast").cache_read, 2.0)

    def test_null_speed_is_treated_as_standard(self):
        """Rows stored before the speed column exists must still price correctly."""
        p = load_prices(write_prices(SIMPLE))
        self.assertEqual(p.resolve("m1", "2026-06-01", None).output, 100.0)


class ComputeCostTests(unittest.TestCase):
    def setUp(self):
        self.p = load_prices(write_prices(SIMPLE))
        self.rates = self.p.resolve("m1", "2026-06-01")

    def test_each_bucket_uses_its_own_rate(self):
        cost, parts = compute_cost(
            row(input_tokens=1_000_000, output_tokens=1_000_000,
                cache_read_tokens=1_000_000, cache_write_5m_tokens=1_000_000,
                cache_write_1h_tokens=1_000_000),
            self.rates, self.p)
        self.assertAlmostEqual(parts["input"], 10.0)
        self.assertAlmostEqual(parts["output"], 100.0)
        self.assertAlmostEqual(parts["cache_read"], 1.0)
        self.assertAlmostEqual(parts["cache_write_5m"], 12.5)
        self.assertAlmostEqual(parts["cache_write_1h"], 20.0)
        self.assertAlmostEqual(cost, 143.5)

    def test_thinking_tokens_are_not_billed_twice(self):
        """thinking_tokens are a subset of output_tokens, not an addition."""
        a, _ = compute_cost(row(output_tokens=1000, thinking_tokens=0), self.rates, self.p)
        b, _ = compute_cost(row(output_tokens=1000, thinking_tokens=900), self.rates, self.p)
        self.assertEqual(a, b)

    def test_web_search_is_charged_per_request(self):
        cost, parts = compute_cost(row(web_search_requests=5), self.rates, self.p)
        self.assertAlmostEqual(parts["web_search"], 0.05)

    def test_web_fetch_is_free(self):
        cost, parts = compute_cost(row(web_fetch_requests=100), self.rates, self.p)
        self.assertEqual(parts["web_fetch"], 0.0)

    def test_zero_usage_costs_nothing(self):
        cost, _ = compute_cost(row(), self.rates, self.p)
        self.assertEqual(cost, 0.0)

    def test_breakdown_records_which_card_was_used(self):
        _, parts = compute_cost(row(output_tokens=1), self.rates, self.p)
        self.assertEqual(parts["_model"], "m1")
        self.assertEqual(parts["_effective_from"], "2000-01-01")


class PricesFileTests(unittest.TestCase):
    def test_rev_is_required(self):
        """Revisions are declared, so the declaration cannot be optional."""
        d = tempfile.mkdtemp()
        with open(f"{d}/prices.json", "w", encoding="utf-8") as fh:
            json.dump({"models": SIMPLE}, fh)
        with self.assertRaises(PriceError) as ctx:
            load_prices(d)
        self.assertIn("rev", str(ctx.exception))

    def test_rev_is_read_from_the_file(self):
        self.assertEqual(load_prices(write_prices(SIMPLE, rev=7)).rev, 7)

    def test_missing_file_raises(self):
        with self.assertRaises(PriceError):
            load_prices(tempfile.mkdtemp())

    def test_incomplete_rate_entry_is_rejected(self):
        d = write_prices({"m1": [{"effective_from": "2000-01-01", "input": 1.0}]})
        with self.assertRaises(PriceError):
            load_prices(d)

    def test_hash_changes_when_a_rate_changes(self):
        """The hash is what decides whether a new price_rev is needed."""
        a = load_prices(write_prices(SIMPLE))
        bumped = {"m1": [{**SIMPLE["m1"][0], "output": 101.0}]}
        b = load_prices(write_prices(bumped))
        self.assertNotEqual(a.content_hash, b.content_hash)

    def test_comments_and_formatting_do_not_change_the_hash(self):
        """Editing prose must not mint a revision or prompt a recost.

        A revision means "the rates changed". Hashing the whole file made
        rewording a note look identical to a price change.
        """
        base = {"models": SIMPLE, "server_tools": {"web_search_per_request": 0.01}}
        reworded = {
            **base,
            "meta": {"note": ["totally different text"], "retrieved": "2099-01-01"},
            "server_tools": {"web_search_per_request": 0.01,
                             "_note": "an explanatory underscore key"},
        }
        self.assertEqual(semantic_hash(base), semantic_hash(reworded))

    def test_key_order_does_not_change_the_hash(self):
        a = {"models": {"x": [{"input": 1, "output": 2}]}, "server_tools": {"b": 1, "a": 2}}
        b = {"server_tools": {"a": 2, "b": 1}, "models": {"x": [{"output": 2, "input": 1}]}}
        self.assertEqual(semantic_hash(a), semantic_hash(b))

    def test_server_tool_rate_change_does_change_the_hash(self):
        a = {"models": SIMPLE, "server_tools": {"web_search_per_request": 0.01}}
        b = {"models": SIMPLE, "server_tools": {"web_search_per_request": 0.02}}
        self.assertNotEqual(semantic_hash(a), semantic_hash(b))

    def test_shipped_prices_file_covers_every_model_in_use(self):
        """The real prices.json must price every model this project has seen."""
        p = load_prices(PROJECT_ROOT)
        for model in ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
                      "claude-fable-5", "claude-haiku-4-5-20251001"):
            self.assertIsNotNone(p.resolve(model, "2026-08-01"),
                                 f"{model} has no rate entry")

    def test_shipped_rates_match_the_published_table(self):
        """Guards against a typo in a rate silently skewing every cost."""
        p = load_prices(PROJECT_ROOT)
        opus = p.resolve("claude-opus-5", "2026-08-01")
        self.assertEqual(
            (opus.input, opus.output, opus.cache_write_5m, opus.cache_write_1h, opus.cache_read),
            (5.0, 25.0, 6.25, 10.0, 0.50))
        haiku = p.resolve("claude-haiku-4-5-20251001", "2026-08-01")
        self.assertEqual(
            (haiku.input, haiku.output, haiku.cache_write_5m, haiku.cache_write_1h, haiku.cache_read),
            (1.0, 5.0, 1.25, 2.0, 0.10))
        fable = p.resolve("claude-fable-5", "2026-08-01")
        self.assertEqual((fable.input, fable.output), (10.0, 50.0))

    def test_shipped_cache_multipliers_hold(self):
        """1.25x / 2.0x / 0.1x of base input, per the published table."""
        p = load_prices(PROJECT_ROOT)
        for model in ("claude-opus-5", "claude-fable-5", "claude-haiku-4-5", "claude-sonnet-5"):
            r = p.resolve(model, "2026-08-01")
            self.assertAlmostEqual(r.cache_write_5m, r.input * 1.25, msg=model)
            self.assertAlmostEqual(r.cache_write_1h, r.input * 2.0, msg=model)
            self.assertAlmostEqual(r.cache_read, r.input * 0.1, msg=model)


if __name__ == "__main__":
    unittest.main(verbosity=2)
