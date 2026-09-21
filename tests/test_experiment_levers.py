"""Reinforcement shaping (trade-anchored reward) and portfolio-state overlay.

These are the two experiment levers: B changes what each dopamine pulse grades
(the consequence of the previous action instead of the one-tick drift), C gives
the fly's visual input access to its own portfolio state. Defaults preserve the
original behavior exactly; the experiment turns them on explicitly.
"""

import time

import numpy as np
import pytest

from stonkfly.config import D, Settings
from stonkfly.display import market_frame
from stonkfly.market import Quote
from stonkfly.ledger import Ledger
from stonkfly.risk import Guard


def test_defaults_preserve_the_original_behavior():
    s = Settings()
    assert s.reward_anchor == "tick"
    assert s.reward_horizon_ticks == 1
    assert s.show_portfolio_state is False


def test_invalid_reward_anchor_is_rejected():
    with pytest.raises(ValueError, match="reward_anchor"):
        Settings(reward_anchor="episode")


@pytest.mark.parametrize("bad", [0, -5, 1.5, True])
def test_invalid_reward_horizon_is_rejected(bad):
    with pytest.raises(ValueError):
        Settings(reward_horizon_ticks=bad)


def test_commit_tick_with_a_held_anchor_keeps_the_reference(tmp_path):
    """Trade-anchored reinforcement: between events the anchor must not drift."""
    s = Settings(products=("BTC-USDT",))
    l = Ledger(tmp_path / "l.sqlite", s, "paper")
    l.put("anchor", "99.5")
    l.commit_tick(None, {"file": "brain-0.npz", "sha256": "x"})
    assert l.get("anchor") == "99.5"          # held
    assert l.get("tick") == 1                 # the tick still committed
    l.commit_tick("100.1", {"file": "brain-0.npz", "sha256": "x"})
    assert l.get("anchor") == "100.1"         # an explicit re-base still lands
    l.close()


def test_settings_adoption_is_explicit_and_recorded(tmp_path):
    """A settings change adopts only through the recorded migration."""
    s = Settings(products=("BTC-USDT",))
    l = Ledger(tmp_path / "l.sqlite", s, "paper")
    l.close()
    changed = Settings(products=("BTC-USDT",), reward_anchor="trade",
                       reward_horizon_ticks=30, show_portfolio_state=True)
    with pytest.raises(RuntimeError, match="mismatch"):
        Ledger(tmp_path / "l.sqlite", changed, "paper")
    adopted = Ledger(tmp_path / "l.sqlite", changed, "paper", adopt_settings=True)
    try:
        trail = adopted.get("settings_migrations")
        assert trail and trail[0]["to"] == changed.signature()
        assert trail[0]["from"] == s.signature()
    finally:
        adopted.close()


@pytest.mark.parametrize("field,value", [
    ("exchange", "coinbase"), ("environment", "live"),
    ("quote_ccy", "USDC"), ("account", "different-account"),
    (None, None),
])
def test_settings_adoption_cannot_bypass_identity(tmp_path, field, value):
    s = Settings(products=("BTC-USDT",))
    identity = {"exchange": "okx", "environment": "okx-demo",
                "quote_ccy": "USDT", "account": "original-account"}
    ledger = Ledger(tmp_path / "l.sqlite", s, "okx-demo", identity=identity)
    try:
        if field is None:
            ledger.put("identity", None)
        requested = dict(identity)
        if field is not None:
            requested[field] = value
        changed = Settings(products=("BTC-USDT",), reward_anchor="trade")
        with pytest.raises(RuntimeError, match="identity|mismatch"):
            Ledger(ledger.path, changed, "okx-demo", identity=requested, adopt_settings=True)
        assert ledger.get("settings") == s.signature()
        assert ledger.get("settings_migrations") is None
        assert ledger.cash == D("100")
    finally:
        ledger.close()


def test_settings_adoption_never_crosses_modes(tmp_path):
    s = Settings(products=("BTC-USDT",))
    l = Ledger(tmp_path / "l.sqlite", s, "paper")
    l.close()
    with pytest.raises(RuntimeError, match="mode"):
        Ledger(tmp_path / "l.sqlite", s, "okx-demo", adopt_settings=True)


def test_market_frame_renders_the_portfolio_overlay():
    q_bid, q_ask = D("81000"), D("81000.1")
    history = [100.0 + i for i in range(50)]
    plain = market_frame("BTC-USDT", history, q_bid, q_ask)
    state = {"equity_history": [100.0 - i * 0.05 for i in range(50)],
             "cash_ratio": 0.3}
    overlay = market_frame("BTC-USDT", history, q_bid, q_ask, state=state)
    assert overlay.shape == plain.shape == (180, 320, 3)
    assert not np.array_equal(overlay, plain)   # the overlay changes the frame
    # the cash bar paints the bottom edge: filled part green up to the ratio,
    # the rest shows the track so the bar's full extent stays visible
    assert tuple(overlay[178, 5]) == (30, 150, 80)
    assert tuple(overlay[178, 319]) == (190, 200, 215)


def test_market_frame_state_handles_degenerate_histories():
    history = [100.0, 100.1]
    base = market_frame("BTC-USDT", history, D("100"), D("100.1"))
    for state in (
        {"equity_history": [], "cash_ratio": None},
        {"equity_history": [100.0], "cash_ratio": 1.0},
        {"equity_history": [5.0, 5.0], "cash_ratio": 5.0},   # flat line, clamped ratio
    ):
        frame = market_frame("BTC-USDT", history, D("100"), D("100.1"), state=state)
        assert frame.shape == (180, 320, 3)
        assert np.isfinite(frame).all()
    assert not np.array_equal(
        market_frame("BTC-USDT", history, D("100"), D("100.1"),
                     state={"equity_history": [5.0, 5.0], "cash_ratio": 0.5}),
        base,
    )


def test_sell_buffer_derives_from_the_published_band(tmp_path):
    """floatPxLmtPct is published per instrument; the buffer adapts to it."""
    s = Settings(products=("BTC-USDT",))
    l = Ledger(tmp_path / "l.sqlite", s, "paper")
    g = Guard(s, l, tmp_path / "STOP")

    def frame(band):
        return Quote(
            "BTC-USDT", D("81060"), D("81060.1"), time.time(),
            D("0.00000001"), None, D("0.1"), D("1"), D("0.00001"),
            float_px_lmt_pct=D(band) if band is not None else None,
        )

    l.put("positions", {"BTC-USDT": "1"})
    # BTC-USDT publishes 0.005: buffer = 40% of the band = 0.002 below the bid.
    sell = g.plan("BTC-USDT", "SELL", {"BTC-USDT": frame("0.005")})
    assert sell["limit_price"] == "80897.8"       # 81060 * 0.998, tick-down
    # a pair with a tighter published band gets a proportionally tighter buffer
    sell_tight = g.plan("BTC-USDT", "SELL", {"BTC-USDT": frame("0.0025")})
    assert sell_tight["limit_price"] == "80978.9"  # band 0.0025 -> buffer 0.001: 81060 * 0.999, tick-down
    # a wider band widens the buffer only up to the configured slippage cap
    sell_wide = g.plan("BTC-USDT", "SELL", {"BTC-USDT": frame("0.02")})
    assert sell_wide["limit_price"] == "80654.7"   # min(0.005, 0.02*0.4) = 0.005: 81060 * 0.995
    # no published coefficient: falls back to the observed 0.002
    sell_default = g.plan(
        "BTC-USDT", "SELL",
        {"BTC-USDT": Quote(
            "BTC-USDT", D("81060"), D("81060.1"), time.time(),
            D("0.00000001"), None, D("0.1"), D("1"), D("0.00001"), None)},
    )
    assert sell_default["limit_price"] == "80897.8"
    l.close()
