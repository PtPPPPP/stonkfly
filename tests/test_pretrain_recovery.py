"""Pretrain crash recovery and cache integrity.

A crash between ``reserve`` and ``execute`` must be recoverable at startup
(PREPARED is provably never-executed in a paper-only run), an UNKNOWN intent
must refuse to continue, a corrupt candle cache must stop instead of changing
the replay dataset, and every abnormal end must fail the exit code. The exchange is
an in-memory double; no network, no credentials."""

import importlib.util
import json
import os
import sqlite3
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INCREMENTS = {"lotSz": "0.00000001", "tickSz": "0.1", "minSz": "0.00001"}


def _tool():
    spec = importlib.util.spec_from_file_location(
        "fly_pretrain", os.path.join(ROOT, "tools", "fly_pretrain.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeOKX:
    """Public-API double: instruments plus candles, served through the real
    read-path contract (``_get``) so the retrying code path is exercised."""

    def __init__(self, closes):
        self.closes = closes
        now = int(time.time() * 1000)
        self.rows = [
            [str(now - i * 60000), "1", "1", "1", str(c), "1", "1", "1", "1"]
            for i, c in enumerate(reversed(closes))
        ]

    def instruments(self, inst_id):
        assert inst_id == "BTC-USDT"
        return {"instId": "BTC-USDT", "lotSz": INCREMENTS["lotSz"],
                "tickSz": INCREMENTS["tickSz"], "minSz": INCREMENTS["minSz"]}

    def _get(self, path, params=None, auth=False, what="query", retries=3):
        assert auth is False and path in (
            "/api/v5/market/candles", "/api/v5/market/history-candles"
        )
        after = int(params["after"]) if params and "after" in params else None
        rows = self.rows
        if after is not None:
            rows = [r for r in rows if int(r[0]) < after]
        return {"code": "0", "data": rows[:300]}


def _closes(n, start=100.0):
    return [
        round(start + 5 * (i % 40) / 40 * (1 if (i // 40) % 2 == 0 else -1), 2)
        for i in range(n)
    ]


@pytest.fixture
def tool():
    return _tool()


def _seed_cache(out, closes):
    out.mkdir(parents=True, exist_ok=True)
    (out / "candles.json").write_text(json.dumps({
        "product": "BTC-USDT", "bar": "1m", "complete": True,
        "increments": INCREMENTS, "closes": closes,
    }) + "\n")


def _seed_intent(tool, out, status):
    """Leave one pending intent in the given status, as a crash would."""
    s = tool.pretrain_settings("BTC-USDT")
    ledger = tool.Ledger(out / "ledger.sqlite", s, "paper")
    try:
        plan = ledger.reserve({"product": "BTC-USDT"}, time.time())
        if status == "UNKNOWN":
            ledger.begin_attempt(plan["client_order_id"])
        return plan["client_order_id"]
    finally:
        ledger.close()


@pytest.mark.full_graph
def test_prepared_residue_is_closed_never_executed_and_run_continues(tool, tmp_path, monkeypatch):
    closes = _closes(200)
    _seed_cache(tmp_path, closes)
    cid = _seed_intent(tool, tmp_path, "PREPARED")
    monkeypatch.setattr(tool, "build_client", lambda: FakeOKX(closes))

    rc = tool.main(["--out", str(tmp_path), "--ticks", "3"])

    assert rc == 0
    s = tool.pretrain_settings("BTC-USDT")
    ledger = tool.Ledger(tmp_path / "ledger.sqlite", s, "paper")
    try:
        row = ledger.db.execute(
            "SELECT status,settlement FROM orders WHERE id=?", (cid,)
        ).fetchone()
        assert row[0] == "REJECTED"  # closed as never-executed, never settled
        assert row[1] is None
        trail = ledger.get("resolved_orders")
        assert trail and trail[0]["reason"] == "virtual_intent_never_executed"
        assert trail[0]["source"] == "pretrain_recovery"
        assert ledger.get("tick") == 3  # the run continued past the recovery
        assert ledger.get("halted") is None
    finally:
        ledger.close()


@pytest.mark.full_graph
def test_unknown_residue_refuses_to_continue(tool, tmp_path, monkeypatch):
    closes = _closes(200)
    _seed_cache(tmp_path, closes)
    cid = _seed_intent(tool, tmp_path, "UNKNOWN")
    monkeypatch.setattr(tool, "build_client", lambda: FakeOKX(closes))

    with pytest.raises(RuntimeError, match="UNKNOWN"):
        tool.main(["--out", str(tmp_path), "--ticks", "3"])

    s = tool.pretrain_settings("BTC-USDT")
    ledger = tool.Ledger(tmp_path / "ledger.sqlite", s, "paper")
    try:
        row = ledger.db.execute(
            "SELECT status FROM orders WHERE id=?", (cid,)
        ).fetchone()
        assert row[0] == "UNKNOWN"  # untouched: no automatic closure
        assert ledger.get("tick") == 0  # no tick ran
    finally:
        ledger.close()


def test_corrupt_candle_cache_refuses_without_refetch(tool, tmp_path, monkeypatch, capsys):
    closes = _closes(200)
    _seed_cache(tmp_path, closes)
    (tmp_path / "candles.json").write_text('{"product": "BTC-USDT", "clo')  # truncated
    monkeypatch.setattr(tool, "build_client", lambda: FakeOKX(closes))

    rc = tool.main(["--out", str(tmp_path), "--ticks", "2"])

    assert rc == 1
    assert "Cannot read candle cache" in capsys.readouterr().out
    assert (tmp_path / "candles.json").read_text() == '{"product": "BTC-USDT", "clo'
    assert not (tmp_path / "ledger.sqlite").exists()
    assert not (tmp_path / "candles.json.tmp").exists()


def test_missing_instrument_fails_the_run_cleanly(tool, tmp_path, monkeypatch):
    class NoInstruments(FakeOKX):
        def instruments(self, inst_id):
            return None

    monkeypatch.setattr(tool, "build_client", lambda: NoInstruments(_closes(50)))
    rc = tool.main(["--out", str(tmp_path), "--bars", "100"])
    assert rc == 1


@pytest.mark.parametrize("change", ["refresh", "missing", "product", "bar"])
def test_existing_ledger_never_switches_its_dataset(tool, tmp_path, change):
    closes = _closes(200)
    _seed_cache(tmp_path, closes)
    _seed_intent(tool, tmp_path, "PREPARED")
    cache = tmp_path / "candles.json"
    if change == "missing":
        cache.unlink()
    before = cache.read_bytes() if cache.exists() else None
    with pytest.raises(RuntimeError):
        tool.load_data(
            None, tmp_path, "ETH-USDT" if change == "product" else "BTC-USDT",
            "5m" if change == "bar" else "1m", 200, change == "refresh",
        )
    assert (cache.read_bytes() if cache.exists() else None) == before


def test_resume_matches_uninterrupted_virtual_clock_and_trades(
    tool, tmp_path, monkeypatch, lightweight_controller,
):
    from stonkfly.audit import read_ledger

    monkeypatch.setattr(tool.time, "time", lambda: 1800000000.0)
    counter = {"n": 0}

    def observe(self, frame, kind):
        side = ("BUY", "SELL")[counter["n"] % 2]
        counter["n"] += 1
        return {"side": side, "stimulus": kind, "memory": {"changed_edges": 0}}

    monkeypatch.setattr(tool.FlyController, "observe", observe)
    settings = tool.pretrain_settings("BTC-USDT")
    closes = [100.0] * 150
    snapshots = []
    for name, chunks in (("continuous", [4]), ("resumed", [2, 2])):
        out = tmp_path / name
        _seed_cache(out, closes)
        counter["n"] = 0
        for ticks in chunks:
            assert tool.run_pretrain(out, settings, closes, ticks, "BTC-USDT") == 0
        view = read_ledger(out)
        snapshots.append({
            "cash": view["meta"]["cash"],
            "positions": view["meta"]["positions"],
            "observation": view["meta"]["observation"],
            "orders": [(o["created"], o["plan"]["side"], o["settlement"]) for o in view["orders"]],
        })
    assert snapshots[0] == snapshots[1]
    assert len(snapshots[1]["orders"]) == 4
    stamps = [row[0] for row in snapshots[1]["orders"]]
    assert all(b - a == 60 for a, b in zip(stamps, stamps[1:]))
