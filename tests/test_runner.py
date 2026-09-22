"""Lifecycle boundaries use real ledgers and injected, offline dependencies."""

import itertools
from dataclasses import replace
from types import SimpleNamespace

import pytest

from stonkfly import cli, locking, runner
from stonkfly.audit import read_ledger
from stonkfly.config import Settings
from stonkfly.ledger import Ledger
from stonkfly.okx_client import TransientReadError


@pytest.mark.parametrize("outcome", ["healthy", "transient", "bug"])
def test_demo_observation_bounds_both_clients_and_always_clears_budget(
    tmp_path, monkeypatch, lightweight_controller, outcome,
):
    from stonkfly.market import FixtureMarket
    from stonkfly.neural.controller import FlyController
    from stonkfly.okx_broker import OKXBroker
    import stonkfly.okx_market as market_module

    account_client = SimpleNamespace(
        read_deadline=None, read_retries_used=0,
        measure_time_offset=lambda samples: {"offset": 0, "rtt": 0, "samples": [0] * samples},
    )
    market_client = SimpleNamespace(read_deadline=None, read_retries_used=0)
    calls = []

    def check_budget():
        assert account_client.read_deadline is not None
        assert account_client.read_deadline == market_client.read_deadline

    def reconcile():
        assert account_client.read_deadline is None
        assert market_client.read_deadline is None

    class Market(FixtureMarket):
        client = market_client

        def __init__(self, products):
            super().__init__(products)
            self.reference = FixtureMarket(("BTC-USDC",))

        def snapshot(self):
            check_budget()
            calls.append("market")
            if len(calls) == 1:
                if outcome == "transient":
                    raise TransientReadError("injected outage")
                if outcome == "bug":
                    raise ValueError("injected programming error")
            quote = self.reference.snapshot()["BTC-USDC"]
            product = self.products[0]
            if not self.history[product]:
                self.history[product] = self.reference.history["BTC-USDC"][:]
            return {product: replace(quote, product=product)}

    broker = SimpleNamespace(
        mode="okx-demo", exchange="okx", client=account_client,
        preflight=lambda: {"mode": "okx-demo"}, reconcile=reconcile,
        verify_balances=check_budget,
    )
    monkeypatch.setattr(OKXBroker, "from_env", lambda settings, ledger: broker)
    monkeypatch.setattr(market_module, "OKXMarket", Market)
    monkeypatch.setattr(runner, "sleep_until", lambda *args: None)
    observe = FlyController.observe

    def observe_after_budget(self, frame, kind):
        reconcile()
        return observe(self, frame, kind)

    monkeypatch.setattr(FlyController, "observe", observe_after_budget)
    argv = ["run", "--okx", "--okx-demo", "--max-order-attempts", "2",
            "--steps", "1", "--out", str(tmp_path)]
    if outcome == "bug":
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 1
    else:
        cli.main(argv)
    reconcile()  # Cleared even on an exception from the market client.
    view = read_ledger(tmp_path)
    assert view["meta"]["tick"] == (0 if outcome == "bug" else 1)
    assert view["orders"] == []
    assert len(calls) == (2 if outcome == "transient" else 1)


def _session(tmp_path):
    settings = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper")
    return runner.RunSession(SimpleNamespace(out=tmp_path), settings, ledger)


@pytest.mark.parametrize("error", [ValueError("bad payload"), TypeError("bug")])
def test_clock_refresh_does_not_swallow_programming_errors(tmp_path, error):
    session = _session(tmp_path)
    session.clock_offset = 0.25

    def measure(samples):
        raise error

    session.clock_client = SimpleNamespace(measure_time_offset=measure)
    try:
        with pytest.raises(type(error)):
            session._refresh_clock()
        assert session.clock_offset == 0.25
    finally:
        session.ledger.close()


def test_transient_clock_outage_is_reported_without_changing_offset(tmp_path, capsys):
    session = _session(tmp_path)
    session.clock_offset = 0.25

    def measure(samples):
        raise TransientReadError("temporary outage")

    session.clock_client = SimpleNamespace(measure_time_offset=measure)
    try:
        session._refresh_clock()
        assert session.clock_offset == 0.25
        assert "clock_recheck_failed" in capsys.readouterr().out
    finally:
        session.ledger.close()


def test_clock_refresh_updates_the_recorded_health_offset(tmp_path):
    session = _session(tmp_path)
    session.clock_client = SimpleNamespace(measure_time_offset=lambda samples: {
        "offset": 0.75, "rtt": 0.1, "samples": [0] * samples,
    })
    try:
        session._refresh_clock()
        assert session.now() - runner.time.time() == pytest.approx(0.75, abs=0.01)
        assert session.ledger.get("clock_offset") == 0.75
    finally:
        session.ledger.close()


def test_lock_is_released_even_when_ledger_close_fails(tmp_path, monkeypatch):
    original_close = Ledger.close

    def fail_close(self):
        original_close(self)
        raise OSError("injected close failure")

    monkeypatch.setattr(Ledger, "close", fail_close)
    with pytest.raises(OSError, match="injected close failure"):
        cli.main(["run", "--fixture", "--preflight-only", "--out", str(tmp_path)])
    lock = locking.acquire(tmp_path / "worker.lock")
    locking.release(lock)


def test_checkpoint_failure_prevents_any_order(tmp_path, monkeypatch, lightweight_controller):
    from stonkfly.neural.controller import FlyController

    def fail_save(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(FlyController, "observe", lambda *args: {
        "side": "BUY", "stimulus": "none", "memory": {"changed_edges": 0},
    })
    monkeypatch.setattr(FlyController, "save", fail_save)
    with pytest.raises(SystemExit):
        cli.main(["run", "--fixture", "--fast", "--steps", "1", "--out", str(tmp_path)])
    view = read_ledger(tmp_path)
    assert view["orders"] == []
    assert view["meta"]["tick"] == 0
    assert view["meta"]["checkpoint"] is None


def test_unknown_submission_stops_after_one_durable_intent(tmp_path, monkeypatch, lightweight_controller):
    from stonkfly.broker import PaperBroker, UnresolvedOrder
    from stonkfly.market import FixtureMarket
    from stonkfly.neural.controller import FlyController

    attempts = []
    snapshot = FixtureMarket.snapshot

    def unchanged_book(self):
        self.tick = 0
        return snapshot(self)

    monkeypatch.setattr(FixtureMarket, "snapshot", unchanged_book)

    def lose_response(self, plan, before_submit):
        before_submit(plan)
        assert self.l.get("tick") == 1
        assert self.l.get("checkpoint") == plan["checkpoint"]
        self.l.begin_attempt(plan["client_order_id"])
        attempts.append(plan)
        raise UnresolvedOrder("injected lost response")

    monkeypatch.setattr(FlyController, "observe", lambda *args: {
        "side": "BUY", "stimulus": "none", "memory": {"changed_edges": 0},
    })
    monkeypatch.setattr(PaperBroker, "execute", lose_response)
    with pytest.raises(SystemExit):
        cli.main(["run", "--fixture", "--fast", "--steps", "3", "--out", str(tmp_path)])
    view = read_ledger(tmp_path)
    assert len(attempts) == 1
    assert len(view["orders"]) == 1
    assert view["orders"][0]["status"] == "UNKNOWN"
    assert view["meta"]["halted"] == "UnresolvedOrder"


def test_wait_never_passes_a_negative_duration_to_sleep(tmp_path, monkeypatch):
    ticks = itertools.chain([0, 2], itertools.repeat(2))
    sleeps = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    runner.sleep_until(1, tmp_path / "STOP")
    assert sleeps == [1]


@pytest.mark.parametrize("speed", ["nan", "inf", "-inf"])
def test_nonfinite_replay_speed_is_rejected_before_start(tmp_path, speed):
    with pytest.raises(SystemExit) as exc:
        cli.main(["watch", "--replay", "--out", str(tmp_path), f"--speed={speed}"])
    assert exc.value.code == 2
    assert list(tmp_path.iterdir()) == []
