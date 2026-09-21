"""Trade-anchored reward semantics: what counts as "a trade" is decided once,
in the ledger settlement, never by matching broker-specific status strings.

Paper reports FILLED, OKX reports SETTLED, and a zero-fill FOK cancellation is
a legitimate SETTLED that moved no money -- none of these may be confused with
each other when the reward anchor is re-based."""

import json
import sqlite3
import sys
import time

import pytest

from stonkfly.broker import PaperBroker
from stonkfly.config import D, Settings
from stonkfly.ledger import Ledger
from stonkfly.market import Quote
from stonkfly.risk import Guard, Veto


def _plan():
    return {
        "product": "BTC-USDC", "side": "BUY", "base_size": "0.01",
        "limit_price": "100.2", "fee_ceiling": "0.01",
        "observed_ask": "100.1", "observed_bid": "100",
    }


@pytest.fixture
def paper(tmp_path):
    s = Settings(reward_anchor="trade")
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "paper")
    guard = Guard(s, ledger, tmp_path / "STOP")
    yield s, ledger, guard
    ledger.close()


def _quote():
    return Quote(
        "BTC-USDC", D("100"), D("100.1"), time.time(),
        D(".00000001"), D(".01"), D(".01"), D("1"), D(".00000001"),
    )


def _reserve_filled(l):
    return l.reserve(_plan(), time.time())["client_order_id"]


def test_paper_fill_is_a_trade(paper):
    _, l, _ = paper
    cid = _reserve_filled(l)
    l.settle(cid, D("0.01"), D("1.0"), D("0.001"))
    assert l.filled_trade(cid) is True


def test_zero_fill_fok_settlement_is_not_a_trade(paper):
    # The OKX path settles a cancelled-unfilled FOK exactly like this.
    _, l, _ = paper
    cid = _reserve_filled(l)
    l.settle(cid, D(0), D(0), D(0), "quote")
    assert l.filled_trade(cid) is False


def test_rejected_intent_is_not_a_trade(paper):
    _, l, _ = paper
    cid = _reserve_filled(l)
    l.mark(cid, "REJECTED")
    assert l.filled_trade(cid) is False


def test_unknown_and_missing_intents_are_not_trades(paper):
    _, l, _ = paper
    cid = _reserve_filled(l)
    l.mark(cid, "UNKNOWN")
    assert l.filled_trade(cid) is False
    assert l.filled_trade("nonexistent") is False


def test_paper_broker_execution_carries_the_intent_id(paper):
    s, l, _ = paper
    broker = PaperBroker(s, l)
    plan = l.reserve(_plan(), time.time())
    result = broker.execute(plan, lambda p: None)
    assert result["status"] == "FILLED"
    assert result["client_order_id"] == plan["client_order_id"]
    assert l.filled_trade(plan["client_order_id"]) is True


def test_trade_anchored_run_rebases_on_a_real_fill(tmp_path, monkeypatch, lightweight_controller):
    """End to end in fixture mode: a filled BUY re-anchors the reward; without
    the semantic layer the paper FILLED status used to be invisible to the
    re-anchor check (which matched only SETTLED)."""
    import stonkfly.cli as cli_mod
    import stonkfly.market as market_mod
    import stonkfly.neural.controller as controller_mod

    forced = {
        "side": "BUY", "KC_spikes": 0, "stimulus": "reward",
        "left_hz": 0.0, "right_hz": 0.0, "difference_hz": 0.0,
        "gate_spikes": 1, "stimulus_ms": 0,
        "memory": {"plastic_edges": 1, "changed_edges": 0, "mean_efficacy": 1.0},
    }
    monkeypatch.setattr(
        controller_mod.FlyController, "observe",
        lambda self, frame, kind: dict(forced),
    )
    # Same price on every call (no slippage veto) but a fresh timestamp each
    # time (the brain load takes longer than the quote-age window).
    def frozen_snapshot(self):
        price = D("60000")
        return {
            "BTC-USDC": Quote(
                "BTC-USDC", price * D(".9995"), price * D("1.0005"), time.time(),
                D("1e-8"), D(".01"), D(".01"), D("1"), D("1e-8"),
            )
        }

    monkeypatch.setattr(market_mod.FixtureMarket, "snapshot", frozen_snapshot)
    monkeypatch.setattr(
        sys, "argv",
        [
            "stonkfly", "run", "--fixture", "--fast",
            "--out", str(tmp_path), "--steps", "1", "--neural-ms", "10",
            "--reward-anchor", "trade",
        ],
    )
    cli_mod.main()

    db = sqlite3.connect(f"file:{tmp_path / 'ledger.sqlite'}?mode=ro", uri=True)
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
    orders = db.execute("SELECT status FROM orders").fetchall()
    db.close()
    assert orders and orders[0][0] == "SETTLED"  # the paper fill, in the ledger
    assert D(meta["anchor"]) != D(meta["initial_cash"])  # re-based after the fill
