"""Loop-level failure-injection tests for the run policy.

The real run loop in cli.main is driven end to end with the exchange replaced
by in-memory doubles: transient READ failures skip the tick and escalate only
after a configured threshold, anything outside the read-only whitelist still
halts, a transient failure before execution vetoes instead of trading, and a
transient preflight failure exits without halting the directory.
"""

import json
import sqlite3
import sys

import pytest

from stonkfly.config import Settings
from stonkfly.ledger import Ledger
from stonkfly.okx_broker import OKXRiskError
from stonkfly.okx_client import OKXTransportError, TransientReadError


def _events(out):
    path = out / "events.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _meta(out):
    db = sqlite3.connect(f"file:{out / 'ledger.sqlite'}?mode=ro", uri=True)
    try:
        return {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
    finally:
        db.close()


def _orders(out):
    db = sqlite3.connect(f"file:{out / 'ledger.sqlite'}?mode=ro", uri=True)
    try:
        return db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        db.close()


@pytest.fixture
def fixture_run(tmp_path, monkeypatch, lightweight_controller):
    """Patch the two injection points of the fixture loop and pin the decoder.

    ``state`` keys:
      verify_fail_once   -- raise this from the next risk sweep, once
      verify_fail_at     -- raise ``verify_fail`` from risk sweep N onward
      snapshot_fail_at   -- raise ``snapshot_fail`` from snapshot N onward
    Snapshot 1 of a tick is the observation; snapshot 2 is the fresh execution
    book. The controller is forced to propose SELL so the execution path is
    reachable deterministically.
    """
    import stonkfly.broker as broker_mod
    import stonkfly.market as market_mod
    import stonkfly.neural.controller as controller_mod

    state = {"verify_calls": 0, "snapshot_calls": 0}

    real_verify = broker_mod.PaperBroker.verify_balances
    real_snapshot = market_mod.FixtureMarket.snapshot

    def verify_balances(self):
        state["verify_calls"] += 1
        if state.get("verify_fail_once"):
            exc = state.pop("verify_fail_once")
            raise exc
        if state.get("verify_fail_at") and state["verify_calls"] >= state["verify_fail_at"]:
            raise state["verify_fail"]
        return real_verify(self)

    def snapshot(self):
        state["snapshot_calls"] += 1
        if state.get("snapshot_fail_at") and state["snapshot_calls"] >= state["snapshot_fail_at"]:
            raise state["snapshot_fail"]
        return real_snapshot(self)

    monkeypatch.setattr(broker_mod.PaperBroker, "verify_balances", verify_balances)
    monkeypatch.setattr(market_mod.FixtureMarket, "snapshot", snapshot)

    forced_neural = {
        "side": "SELL",
        "KC_spikes": 0,
        "stimulus": "reward",
        "left_hz": 0.0,
        "right_hz": 0.0,
        "difference_hz": 0.0,
        "gate_spikes": 1,
        "stimulus_ms": 0,
        "memory": {"plastic_edges": 1, "changed_edges": 0, "mean_efficacy": 1.0},
    }
    monkeypatch.setattr(
        controller_mod.FlyController,
        "observe",
        lambda self, frame, kind: dict(forced_neural),
    )
    return state


def _argv(tmp_path, steps):
    return [
        "stonkfly", "run", "--fixture", "--fast",
        "--out", str(tmp_path), "--steps", str(steps), "--neural-ms", "10",
    ]


def test_transient_read_failure_skips_the_tick_then_recovers(tmp_path, monkeypatch, fixture_run):
    from stonkfly import cli

    state = fixture_run
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, steps=2))
    # First risk sweep fails transiently; everything afterwards is healthy.
    state["verify_fail_once"] = TransientReadError("No completed candles")

    cli.main()

    meta = _meta(tmp_path)
    assert meta["tick"] == 2
    assert meta["halted"] is None
    events = _events(tmp_path)
    skips = [e for e in events if e.get("type") == "tick_skipped"]
    assert len(skips) == 1
    assert skips[0]["consecutive"] == 1
    assert skips[0]["component"] == "balance/risk sweep"
    assert skips[0]["error"] == "TransientReadError"
    assert [e.get("tick") for e in events if "neural" in e] == [1, 2]
    # A skipped tick trades nothing and leaves no intent behind.
    assert _orders(tmp_path) == 0


def test_consecutive_read_failures_escalate_to_halt(tmp_path, monkeypatch, fixture_run):
    from stonkfly import cli

    state = fixture_run
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, steps=3))
    # Every risk sweep fails: the loop must skip read_fail_halt_after (5)
    # consecutive ticks, then halt instead of skipping forever.
    state["verify_fail_at"] = 1
    state["verify_fail"] = OKXTransportError("connection lost")

    with pytest.raises(SystemExit):
        cli.main()

    meta = _meta(tmp_path)
    assert meta["halted"].startswith("Read-only data unavailable for 5 consecutive ticks")
    assert meta["tick"] == 0
    skips = [e for e in _events(tmp_path) if e.get("type") == "tick_skipped"]
    assert [s["consecutive"] for s in skips] == [1, 2, 3, 4, 5]
    diagnostic = json.loads((tmp_path / "error.json").read_text())
    assert diagnostic["type"] == "RuntimeError"
    assert "Read-only data unavailable" in diagnostic["reason"]
    assert _orders(tmp_path) == 0


def test_failure_outside_the_readonly_whitelist_does_not_skip(tmp_path, monkeypatch, fixture_run):
    """An account-state error must halt immediately, never be treated as a
    transient read failure -- the whitelist is narrow by construction."""
    from stonkfly import cli

    state = fixture_run
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, steps=3))
    state["verify_fail_at"] = 1
    state["verify_fail"] = OKXRiskError("External balance change")

    with pytest.raises(SystemExit):
        cli.main()

    assert _events(tmp_path) == []  # nothing was skipped or committed
    diagnostic = json.loads((tmp_path / "error.json").read_text())
    assert diagnostic["type"] == "OKXRiskError"
    assert _orders(tmp_path) == 0


def test_transient_snapshot_failure_before_execution_vetoes(tmp_path, monkeypatch, fixture_run):
    """The fresh execution snapshot is fetched before any intent exists: if it
    fails transiently the action is refused as a VETO row -- never traded on
    the stale observation quote, and nothing is left unresolved."""
    from stonkfly import cli

    state = fixture_run
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, steps=1))
    # Call 1 = observation snapshot (healthy), call 2 = execution snapshot.
    state["snapshot_fail_at"] = 2
    state["snapshot_fail"] = OKXTransportError("connection lost")

    cli.main()

    meta = _meta(tmp_path)
    assert meta["tick"] == 1
    assert meta["halted"] is None
    rows = [e for e in _events(tmp_path) if "neural" in e]
    assert rows[0]["execution"]["status"] == "VETO"
    assert "transient market-data failure" in rows[0]["execution"]["reason"]
    assert _orders(tmp_path) == 0


def test_settings_mismatch_refuses_cleanly_without_traceback(tmp_path, monkeypatch, capsys):
    """A directory bound to a different Settings signature must refuse with a
    readable message and exit 1 -- not crash with a raw traceback out of main
    (this exact crash surfaced in the live autostart log during the fix round)."""
    from stonkfly import cli

    Ledger(tmp_path / "ledger.sqlite", Settings(products=("BTC-USDT",), capital="50"), "paper").close()
    monkeypatch.setattr(
        sys, "argv", ["stonkfly", "run", "--fixture", "--out", str(tmp_path)]
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert "refused this start" in capsys.readouterr().err
    assert _meta(tmp_path).get("halted") is None  # nothing was written


def test_transient_preflight_failure_exits_without_halting(tmp_path, monkeypatch, demo_credentials):
    """A proxy blip during an unattended restart must not permanently halt an
    existing directory: preflight retries happen on the next launch instead."""
    from stonkfly import cli
    import stonkfly.okx_broker as broker_mod

    monkeypatch.setattr(
        sys, "argv",
        [
            "stonkfly", "run", "--okx", "--okx-demo", "--preflight-only",
            "--out", str(tmp_path), "--max-order-attempts", "2",
        ],
    )

    def flaky(self):
        raise OKXTransportError("connection lost")

    monkeypatch.setattr(broker_mod.OKXBroker, "preflight", flaky)
    with pytest.raises(SystemExit):
        cli.main()

    reopened = Ledger(tmp_path / "ledger.sqlite", Settings(products=("BTC-USDT",)), "okx-demo")
    try:
        assert reopened.get("halted") is None
    finally:
        reopened.close()
    diagnostic = json.loads((tmp_path / "error.json").read_text())
    assert "not halted" in diagnostic["reason"]


def test_clock_offset_beyond_limit_refuses_the_run(tmp_path, monkeypatch, capsys):
    """Preflight measures the OKX clock offset and refuses beyond the limit,
    without halting a directory that has not started."""
    from stonkfly import cli
    import stonkfly.okx_broker as broker_mod
    import stonkfly.okx_client as client_mod

    for var in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
        monkeypatch.setenv(var, "test")
    monkeypatch.setattr(
        sys, "argv",
        [
            "stonkfly", "run", "--okx", "--okx-demo", "--preflight-only",
            "--out", str(tmp_path), "--max-order-attempts", "2",
        ],
    )

    def preflight(self):
        return {"mode": "okx-demo", "account_bound": True, "algo_coverage": "verified"}

    monkeypatch.setattr(broker_mod.OKXBroker, "preflight", preflight)
    monkeypatch.setattr(
        client_mod.OKXClient,
        "measure_time_offset",
        lambda self, samples=5, local_clock=None, mono=None: {
            "offset": 9.0, "rtt": 0.12, "samples": [(0.12, 9.0)] * int(samples),
        },
    )
    with pytest.raises(SystemExit):
        cli.main()

    diagnostic = json.loads((tmp_path / "error.json").read_text())
    assert diagnostic["type"] == "RuntimeError"
    assert "clock" in diagnostic["reason"]
    reopened = Ledger(tmp_path / "ledger.sqlite", Settings(products=("BTC-USDT",)), "okx-demo")
    try:
        assert reopened.get("halted") is None
    finally:
        reopened.close()


def test_clock_offset_within_limit_is_reported(tmp_path, monkeypatch, capsys):
    from stonkfly import cli
    import stonkfly.okx_broker as broker_mod
    import stonkfly.okx_client as client_mod

    for var in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
        monkeypatch.setenv(var, "test")
    monkeypatch.setattr(
        sys, "argv",
        [
            "stonkfly", "run", "--okx", "--okx-demo", "--preflight-only",
            "--out", str(tmp_path), "--max-order-attempts", "2",
        ],
    )

    def preflight(self):
        return {"mode": "okx-demo", "account_bound": True, "algo_coverage": "verified"}

    monkeypatch.setattr(broker_mod.OKXBroker, "preflight", preflight)
    seen = {}

    def measure(self, samples=5, local_clock=None, mono=None):
        seen["samples"] = samples
        return {"offset": 0.25, "rtt": 0.4, "samples": [(0.4, 0.25)] * samples}

    monkeypatch.setattr(client_mod.OKXClient, "measure_time_offset", measure)
    cli.main()

    out = capsys.readouterr().out
    assert '"clock_offset_seconds": 0.25' in out
    assert '"clock_rtt_seconds": 0.4' in out
    assert seen["samples"] == 5
    # A plain preflight-only run stops before trading and records no halt.
    assert _meta(tmp_path).get("halted") is None
