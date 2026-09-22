"""Ops-hardening failure injection: UNKNOWN evidence horizon, tick read
budget, events rotation, warmup boundaries, UTC quota semantics, health
snapshot and observer guards. The exchange is an in-memory double throughout."""

import json
import sqlite3
import sys
import time

import pytest

from okx_doubles import ACCOUNT_CONFIG, detail as _detail, risk_snapshot
from stonkfly.config import D, Settings
from stonkfly.ledger import Ledger
from stonkfly.okx_broker import (
    OKXBroker,
    OKXRiskError,
    _EVIDENCE_HORIZON_SECONDS,
    adjudication_basis,
)
from stonkfly.okx_client import OKXClient, OKXTransportError, TransientReadError

from test_okx import FakeOKX


# ---------------------------------------------------------------------------
# P2-1: UNKNOWN evidence horizon -- absence of evidence is not evidence of
# absence once the exchange's own archive window has passed.
# ---------------------------------------------------------------------------

def _findings(age_seconds, complete=True, history=(), archive=()):
    return {
        "client_order_id": "cid",
        "investigated_at": 0.0,
        "intent_created": -age_seconds,
        "age_seconds": age_seconds,
        "lookup": None,
        "open_orders": [],
        "history": list(history),
        "history_rows": len(history),
        "history_complete": complete,
        "archive": list(archive),
        "archive_rows": len(archive),
        "archive_complete": complete,
    }


def _plan():
    return {
        "order_type": "limit_limit_fok", "product": "BTC-USDT",
        "side": "BUY", "base_size": "0.001", "limit_price": "100.2",
        "fee_ceiling": "0.01", "observed_ask": "100.1", "observed_bid": "100",
    }


@pytest.mark.parametrize(
    "age,expect_allowed",
    [
        (86400, True),                                  # 1 day: fully provable
        (_EVIDENCE_HORIZON_SECONDS - 3600, True),       # just inside the horizon
        (_EVIDENCE_HORIZON_SECONDS + 3600, False),      # past it: unverifiable
        (200 * 86400, False),                           # far past it
    ],
)
def test_adjudication_evidence_horizon(age, expect_allowed):
    allowed, basis = adjudication_basis(_plan(), time.time() - age, _findings(age))
    assert allowed is expect_allowed
    if not expect_allowed:
        assert "evidence horizon" in basis
        assert "stays UNKNOWN" in basis
    else:
        assert "evidence horizon" not in basis


def test_adjudication_still_refuses_visible_or_incomplete_scans():
    f = _findings(86400)
    f["lookup"] = {"ordId": "x"}
    allowed, basis = adjudication_basis(_plan(), time.time() - 86400, f)
    assert not allowed and "visible" in basis
    f = _findings(86400, complete=False)
    allowed, basis = adjudication_basis(_plan(), time.time() - 86400, f)
    assert not allowed and "did not complete" in basis


def _okx_ledger(tmp_path, settings):
    return Ledger(
        tmp_path / "ledger.sqlite", settings, "okx-demo",
        identity={
            "exchange": "okx", "environment": "okx-demo",
            "account": "uid-1", "quote_ccy": "USDT",
        },
    )


def _demo_initialized(ledger, broker):
    """Seed the demo bookkeeping against the fake's actual balances, exactly
    as the first preflight would."""
    details = broker._details()
    cash = broker._cash_balances(details)
    ledger.put("baseline", {k: str(v) for k, v in cash.items()})
    ledger.put("budget", "100")
    ledger.put("cash", "100")
    ledger.put("demo_initialized", True)


def test_aged_unknown_intent_is_never_auto_closed(tmp_path):
    """A clean scan of an intent past the evidence horizon must refuse the
    adjudication and leave the intent exactly as it was."""
    s = Settings(products=("BTC-USDT",))
    ledger = _okx_ledger(tmp_path, s)
    try:
        broker = OKXBroker(s, ledger, FakeOKX())
        _demo_initialized(ledger, broker)
        old = time.time() - (_EVIDENCE_HORIZON_SECONDS + 86400)
        cid = ledger.reserve(_plan(), old)["client_order_id"]
        ledger.mark(cid, "UNKNOWN")
        with pytest.raises(OKXRiskError, match="evidence horizon"):
            broker.adjudicate_absent(cid)
        row = ledger.db.execute(
            "SELECT status FROM orders WHERE id=?", (cid,)
        ).fetchone()
        assert row[0] == "UNKNOWN"
        assert ledger.get("resolved_orders") is None
    finally:
        ledger.close()


def test_adjudication_transport_failure_leaves_intent_unknown(tmp_path):
    """A network error during the investigation is not evidence either."""
    s = Settings(products=("BTC-USDT",))
    ledger = _okx_ledger(tmp_path, s)
    try:
        fake = FakeOKX()

        def dead(*a, **kw):
            raise OKXTransportError("connection lost")

        fake.get_order = dead
        broker = OKXBroker(s, ledger, fake)
        _demo_initialized(ledger, broker)
        cid = ledger.reserve(_plan(), time.time())["client_order_id"]
        ledger.mark(cid, "UNKNOWN")
        with pytest.raises(OKXTransportError):
            broker.adjudicate_absent(cid)
        row = ledger.db.execute(
            "SELECT status FROM orders WHERE id=?", (cid,)
        ).fetchone()
        assert row[0] == "UNKNOWN"
    finally:
        ledger.close()


def test_recent_unknown_adjudicates_absent(tmp_path):
    """Inside the horizon a complete clean scan still closes the intent, with
    the audit record naming the operator judgement."""
    s = Settings(products=("BTC-USDT",))
    ledger = _okx_ledger(tmp_path, s)
    try:
        broker = OKXBroker(s, ledger, FakeOKX())
        _demo_initialized(ledger, broker)
        cid = ledger.reserve(_plan(), time.time() - 3600)["client_order_id"]
        ledger.mark(cid, "UNKNOWN")
        record = broker.adjudicate_absent(cid)
        assert record["confirmation"] == "human_adjudication_not_exchange_confirmation"
        row = ledger.db.execute(
            "SELECT status FROM orders WHERE id=?", (cid,)
        ).fetchone()
        assert row[0] == "REJECTED"
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# Dead code: the duplicated ordId check is gone; the single remaining check
# still refuses an accepted order without an unambiguous id.
# ---------------------------------------------------------------------------

def test_accepted_order_without_ordid_stays_unresolved(tmp_path):
    s = Settings(products=("BTC-USDT",))
    ledger = _okx_ledger(tmp_path, s)
    try:
        fake = FakeOKX()
        fake.place_response = {"code": "0", "data": [{"sCode": "0"}]}  # no ordId
        broker = OKXBroker(s, ledger, fake)
        _demo_initialized(ledger, broker)
        from stonkfly.broker import UnresolvedOrder

        cid = ledger.reserve(_plan(), time.time())["client_order_id"]
        ledger.mark(cid, "PREPARED")
        with pytest.raises(UnresolvedOrder, match="order id"):
            broker.execute(dict(_plan(), client_order_id=cid), lambda p: None)
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# P2-3: per-tick read budget at the client level: observation GETs respect the
# deadline; the submission path (place_order) and order queries never do.
# ---------------------------------------------------------------------------

class ScriptedTransport:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(script, **kw):
    t = ScriptedTransport(script)
    return OKXClient(api_key="k", secret="s", passphrase="p", transport=t, **kw), t


def test_read_deadline_refuses_observation_gets(monkeypatch):
    c, t = _client([])
    c.read_deadline = time.monotonic() - 1  # already spent
    with pytest.raises(TransientReadError) as excinfo:
        c.ticker("BTC-USDT")
    assert excinfo.value.deadline is True
    assert t.calls == []  # the request never even started


def test_read_deadline_never_touches_submission_or_order_queries():
    c, t = _client(
        [
            (200, {"code": "0", "data": [{"sCode": "0", "ordId": "o1"}]}),
            (200, {"code": "0", "data": [{"ordId": "o1", "state": "filled"}]}),
        ]
    )
    c.read_deadline = time.monotonic() - 1  # spent -- and irrelevant here
    resp = c.place_order({"instId": "BTC-USDT"})
    assert resp["data"][0]["ordId"] == "o1"
    order = c.get_order("BTC-USDT", ord_id="o1")
    assert order["ordId"] == "o1"
    assert len(t.calls) == 2  # both went out despite the spent deadline


def test_read_deadline_recovers_when_cleared():
    c, t = _client([(200, {"code": "0", "data": []})])
    c.read_deadline = time.monotonic() - 1
    with pytest.raises(TransientReadError):
        c.ticker("BTC-USDT")
    c.read_deadline = None
    assert c.ticker("BTC-USDT") is None
    assert len(t.calls) == 1


def test_retry_counter_counts_bounded_retries():
    c, t = _client(
        [
            OKXTransportError("connection lost"),
            OKXTransportError("connection lost"),
            (200, {"code": "0", "data": []}),
        ]
    )
    assert c.ticker("BTC-USDT") is None
    assert c.read_retries_used == 2


# ---------------------------------------------------------------------------
# P3-A: events.jsonl size rotation.
# ---------------------------------------------------------------------------

def _big_events(out, megabytes):
    row = json.dumps({"tick": 1, "wall_time": time.time(), "pad": "x" * 64})
    path = out / "events.jsonl"
    with path.open("w", encoding="utf-8") as f:
        while path.stat().st_size < megabytes * 1024 * 1024:
            f.write(row + "\n")


def test_events_rotation_keeps_generations(tmp_path):
    from stonkfly.run_state import rotate_events

    s = Settings(events_rotate_mb=1, events_keep=3)
    out = tmp_path
    _big_events(out, 1)
    (out / "events.1.jsonl").write_text("older")
    rotate_events(out, s)
    assert (out / "events.1.jsonl").read_text() != "older"  # current moved in
    assert (out / "events.2.jsonl").read_text() == "older"  # generation shifted
    assert not (out / "events.jsonl").exists()


def test_events_rotation_failure_is_logged_not_fatal(tmp_path, capsys):
    from stonkfly.run_state import rotate_events

    s = Settings(events_rotate_mb=1, events_keep=3)
    out = tmp_path
    _big_events(out, 1)
    real_replace = type(out / "events.jsonl").replace

    def boom(self, target):
        raise OSError("file held open by a reader")

    import pathlib

    monkey = pytest.MonkeyPatch()
    monkey.setattr(pathlib.Path, "replace", boom)
    try:
        rotate_events(out, s)  # must not raise
    finally:
        monkey.undo()
    assert "rotation failed" in capsys.readouterr().out
    assert (out / "events.jsonl").exists()  # nothing lost, retried next tick
    assert real_replace  # sanity: the real replace is untouched afterwards


def test_events_rotation_runs_inside_the_loop(tmp_path, monkeypatch, lightweight_controller):
    from stonkfly import cli

    s = Settings(events_rotate_mb=1, events_keep=2)
    real = cli.Settings

    def patched(**kw):
        return real(**{**kw, "events_rotate_mb": 1, "events_keep": 2})

    monkeypatch.setattr(cli, "Settings", patched)
    _big_events(tmp_path, 1)
    monkeypatch.setattr(
        sys, "argv",
        ["stonkfly", "run", "--fixture", "--fast",
         "--out", str(tmp_path), "--steps", "1", "--neural-ms", "10"],
    )
    cli.main()
    assert (tmp_path / "events.1.jsonl").exists()


# ---------------------------------------------------------------------------
# Warmup: the frame draws history[-100:], so acting below that is acting on a
# chart the fly can only partially see.
# ---------------------------------------------------------------------------

def test_warmup_boundary_matrix():
    from stonkfly.runner import needs_warmup

    s = Settings(warmup_candles=100)
    assert needs_warmup(0, s) is True
    assert needs_warmup(1, s) is True
    assert needs_warmup(99, s) is True
    assert needs_warmup(100, s) is False
    assert needs_warmup(101, s) is False


def test_warmup_ticks_commit_nothing_and_submit_nothing(tmp_path, monkeypatch, lightweight_controller):
    import stonkfly.market as market_mod

    from stonkfly import cli

    real = cli.Settings

    def patched(**kw):
        return real(**{**kw, "warmup_candles": 115})

    monkeypatch.setattr(cli, "Settings", patched)
    # Simulate a thin exchange history: trim the fixture's seed once, then let
    # it grow by one recorded observation per tick until the threshold clears.
    real_snapshot = market_mod.FixtureMarket.snapshot
    trimmed = {"done": False}

    def thin_snapshot(self):
        quotes = real_snapshot(self)
        if not trimmed["done"]:
            for p in self.history:
                self.history[p] = self.history[p][:90]
            trimmed["done"] = True
        return quotes

    monkeypatch.setattr(market_mod.FixtureMarket, "snapshot", thin_snapshot)
    monkeypatch.setattr(
        sys, "argv",
        ["stonkfly", "run", "--fixture", "--fast",
         "--out", str(tmp_path), "--steps", "2", "--neural-ms", "10"],
    )
    cli.main()

    db = sqlite3.connect(f"file:{tmp_path / 'ledger.sqlite'}?mode=ro", uri=True)
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
    orders = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    db.close()
    assert meta["tick"] == 2  # the run completed its steps after warmup
    assert meta["warmup"] is None  # cleared once history cleared the threshold
    events = [
        json.loads(l)
        for l in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    warmups = [e for e in events if e.get("type") == "warming_up"]
    assert warmups and warmups[0]["candles"] == 91 and warmups[0]["required"] == 115
    assert len(warmups) == 115 - 91  # one recorded wait per missing candle
    committed = [e for e in events if "neural" in e]
    assert len(committed) == 2


# ---------------------------------------------------------------------------
# UTC quota semantics: the attempt budget is spent at begin_attempt (the
# submission moment); the daily fill cap is keyed on intent creation day.
# ---------------------------------------------------------------------------

def test_utc_day_boundary_quota_semantics(tmp_path):
    s = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "paper")
    try:
        day_start = 1800000000 - (1800000000 % 86400)
        created = day_start + 86340           # 23:59 UTC
        next_day = day_start + 86400 + 60     # 00:01 UTC next day
        plan = ledger.reserve(
            {
                "product": "BTC-USDC", "side": "BUY", "base_size": "0.01",
                "limit_price": "100.2", "fee_ceiling": "0.01",
                "observed_ask": "100.1", "observed_bid": "100",
            },
            created,
        )
        cid = plan["client_order_id"]
        ledger.begin_attempt(cid)             # the submission: attempt spent here
        assert ledger.attempts_used() == 1
        ledger.settle(cid, D("0.01"), D("1.0"), D("0.001"))
        # The fill lands in the creation day's bucket; a 23:59 intent that
        # fills at 00:01 escapes that one day's fill cap -- documented edge,
        # bounded by the lifetime attempt budget, which can never escape.
        assert ledger.filled_today(created) == 1
        assert ledger.filled_today(next_day) == 0
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# Health snapshot: derived, read-only, shared by status/serve.
# ---------------------------------------------------------------------------

def _seed_run(tmp_path, meta_rows, events):
    db = sqlite3.connect(tmp_path / "ledger.sqlite")
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY,status TEXT NOT NULL,created REAL NOT NULL,plan TEXT NOT NULL,exchange_id TEXT,settlement TEXT)")
    db.execute("DELETE FROM meta")
    for k, v in meta_rows.items():
        db.execute("INSERT INTO meta VALUES (?,?)", (k, json.dumps(v)))
    db.commit()
    db.close()
    with (tmp_path / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_health_reflects_worker_states(tmp_path):
    from stonkfly.watch import read_health

    now = time.time()
    _seed_run(
        tmp_path,
        {"tick": 5, "halted": None, "clock_offset": 0.2},
        [{"tick": 5, "wall_time": now - 30, "neural": {"side": "HOLD"},
          "execution": {"status": "HOLD"}, "tick_duration_ms": 4210,
          "readonly_duration_ms": 21550}],
    )
    h = read_health(tmp_path)
    assert h["worker"] == "running"
    assert h["network"] == "ok"
    assert h["clock"] == "ok"
    assert h["ledger"] == "ok"
    assert h["tick_duration_ms"] == 4210
    assert h["consecutive_read_failures"] == 0
    assert h["last_tick_utc"].endswith("Z")

    _seed_run(
        tmp_path,
        {"tick": 6, "halted": "Read-only data unavailable for 5 consecutive ticks"},
        [
            {"type": "tick_skipped", "wall_time": now - 10,
             "consecutive": 5, "component": "market data",
             "deadline_exhausted": False},
        ],
    )
    h = read_health(tmp_path)
    assert h["worker"] == "halted"
    assert h["ledger"] == "halted"
    assert h["network"] == "down"
    assert h["consecutive_read_failures"] == 5


def test_health_flags_warmup_and_stale_worker(tmp_path):
    from stonkfly.watch import read_health

    _seed_run(
        tmp_path,
        {"tick": 0, "warmup": {"candles": 42, "required": 100}},
        [{"type": "warming_up", "wall_time": time.time() - 5,
          "candles": 42, "required": 100}],
    )
    h = read_health(tmp_path)
    assert h["worker"] == "warming_up"
    assert h["warmup"] == {"candles": 42, "required": 100}

    _seed_run(tmp_path, {"tick": 9}, [])  # nothing recent, no events
    h = read_health(tmp_path)
    assert h["worker"] == "stopped"
    assert h["network"] == "unknown"


# ---------------------------------------------------------------------------
# Observer guards: status and the shared helper survive missing/corrupt state.
# ---------------------------------------------------------------------------

def test_status_and_helpers_survive_corrupt_ledger(tmp_path, monkeypatch, capsys):
    from stonkfly import cli
    from stonkfly.watch import read_health, read_meta, read_pending_count, read_status

    (tmp_path / "ledger.sqlite").write_bytes(b"this is not a database")
    assert read_meta(tmp_path) == {}
    assert read_status(tmp_path) == {}
    assert read_pending_count(tmp_path) is None
    h = read_health(tmp_path)
    assert h["ledger"] == "unknown"

    monkeypatch.setattr(
        sys, "argv", ["stonkfly", "status", "--out", str(tmp_path)]
    )
    cli.main()  # no crash, no traceback
    out = capsys.readouterr().out
    assert '"health"' in out
