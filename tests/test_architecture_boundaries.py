"""Cross-module accounting, recovery and observation regressions. No network."""

import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from stonkfly import audit, locking
from stonkfly.broker import CoinbaseBroker, PaperBroker
from stonkfly.config import D, Settings
from stonkfly.ledger import AttemptLimitReached, Ledger, MigrationNotPromoted
from stonkfly.risk import Guard, Veto
from test_execution import SDK, quote
from test_okx_checkpoint import _promotion_module


def _plan(side="BUY", product="BTC-USDC"):
    return {
        "product": product, "side": side, "base_size": "0.01",
        "limit_price": "100.2", "fee_ceiling": "1",
        "observed_ask": "100.1", "observed_bid": "100", "quote_timestamp": time.time(),
    }


@pytest.mark.parametrize("status", ["missing", "UNKNOWN", "ACCEPTED", "SETTLED", "REJECTED"])
def test_begin_attempt_cannot_reopen_or_invent_an_intent(tmp_path, status):
    ledger = Ledger(tmp_path / "ledger.sqlite", Settings(), "paper")
    try:
        cid = ledger.reserve(_plan(), time.time())["client_order_id"]
        if status == "SETTLED":
            ledger.settle(cid, ".01", "1", "0")
        elif status != "missing":
            ledger.mark(cid, status)
        target = "not-in-ledger" if status == "missing" else cid
        before = audit.read_ledger(ledger.path)
        with pytest.raises(RuntimeError, match="PREPARED"):
            ledger.begin_attempt(target)
        assert audit.read_ledger(ledger.path) == before
    finally:
        ledger.close()


@pytest.mark.parametrize("kind", ["paper", "coinbase", "okx"])
def test_pre_submit_failure_never_erases_an_unknown_outcome(tmp_path, kind):
    from stonkfly.okx_broker import OKXBroker
    from test_okx import FakeOKX

    settings = Settings(products=("BTC-USDT",)) if kind == "okx" else Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper")
    try:
        if kind == "paper":
            broker = PaperBroker(settings, ledger)
        elif kind == "coinbase":
            broker = CoinbaseBroker(settings, ledger, SDK(), "test-portfolio")
        else:
            broker = OKXBroker(settings, ledger, FakeOKX())
        broker.preflight()
        plan = ledger.reserve(_plan(product=settings.products[0]), time.time())
        ledger.begin_attempt(plan["client_order_id"])

        def refuse(plan):
            raise Veto("already crossed send boundary")

        with pytest.raises(Veto):
            broker.execute(plan, refuse)
        assert ledger.pending()[0]["status"] == "UNKNOWN"
        assert ledger.attempts_used() == 1
    finally:
        ledger.close()


@pytest.mark.parametrize("kind", ["paper", "coinbase"])
def test_every_broker_spends_the_persisted_attempt_budget(tmp_path, kind):
    settings = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper")
    sdk = SDK()
    try:
        broker = PaperBroker(settings, ledger) if kind == "paper" else CoinbaseBroker(
            settings, ledger, sdk, "test-portfolio",
        )
        broker.preflight()
        ledger.set_attempt_limit(1)
        first = ledger.reserve(_plan(), time.time())
        broker.execute(first, lambda p: None)
        second = ledger.reserve(_plan(), time.time())
        with pytest.raises(AttemptLimitReached):
            broker.execute(second, lambda p: None)
        assert ledger.attempts_used() == 1
        assert ledger.filled_trade(first["client_order_id"])
        assert not ledger.pending()
        assert sdk.submissions == (1 if kind == "coinbase" else 0)
    finally:
        ledger.close()


def test_coinbase_binds_portfolio_before_any_reconciliation(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "ledger.sqlite", Settings(), "live")
    ledger.bind_account("previous-portfolio")
    broker = CoinbaseBroker(Settings(), ledger, SDK(), "test-portfolio")
    monkeypatch.setattr(broker, "reconcile", lambda: pytest.fail("wrong account reconciled"))
    try:
        with pytest.raises(RuntimeError, match="Account identity mismatch"):
            broker.preflight()
        assert ledger.get("identity")["account"] == "previous-portfolio"
    finally:
        ledger.close()


def test_prepared_plan_cannot_change_before_submission(tmp_path):
    settings = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper")
    guard = Guard(settings, ledger, tmp_path / "STOP")
    try:
        plan = ledger.reserve(guard.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
        plan["base_size"] = "1000"
        with pytest.raises(Veto, match="ownership"):
            guard.before_submit(plan)
        assert ledger.pending()[0]["plan"]["base_size"] != "1000"
    finally:
        ledger.close()


@pytest.mark.parametrize("difference", ["account", "side"])
def test_union_does_not_merge_distinct_orders(difference):
    ident = {"exchange": "okx", "environment": "okx-demo", "account": "account-a"}
    order = {"id": "client", "exchange_id": "exchange", "status": "SETTLED",
             "plan": {"product": "BTC-USDT", "side": "BUY"},
             "settlement": {"base": "1", "quote": "100", "fee": "0"}}
    other = json.loads(json.dumps(order))
    ident2 = dict(ident)
    if difference == "account":
        ident2["account"] = "account-b"
    else:
        other["plan"]["side"] = "SELL"
    union, conflicts = audit.union_orders([
        {"path": "a", "meta": {"identity": ident}, "orders": [order]},
        {"path": "b", "meta": {"identity": ident2}, "orders": [other]},
    ])
    assert len(union) == (2 if difference == "account" else 1)
    assert len(conflicts) == (0 if difference == "account" else 1)


@pytest.fixture
def migration(tmp_path, monkeypatch):
    tool = _promotion_module()
    monkeypatch.setattr(tool, "_ROOT", tmp_path)
    source = tmp_path / "runs" / "source"
    target = tmp_path / "runs" / "target"
    settings = Settings(products=("BTC-USDT",))
    identity = {"exchange": "okx", "environment": "okx-demo", "account": "demo-account", "quote_ccy": "USDT"}
    ledger = Ledger(source / "ledger.sqlite", settings, "okx-demo", identity=identity)
    ledger.put("baseline", {"USDT": "1000", "BTC": "1"})
    ledger.put("budget", "100")
    for side, size, value, price, fee in (
        ("BUY", ".2", "20", "100", ".001"), ("SELL", ".1", "12", "120", ".002"),
    ):
        plan = {**_plan(side, "BTC-USDT"), "base_size": size, "limit_price": price}
        plan = ledger.reserve(plan, time.time())
        cid = plan["client_order_id"]
        ledger.begin_attempt(cid)
        ledger.mark(cid, "ACCEPTED", side)
        ledger.settle(cid, size, value, fee, "base")
    content = b"fixture checkpoint"
    (source / "brain-0.npz").write_bytes(content)
    ledger.put("checkpoint", {"file": "brain-0.npz", "sha256": hashlib.sha256(content).hexdigest()})
    ledger.close()
    calls = []

    class Client:
        uid = "demo-account"

        def account_config(self):
            calls.append("identity")
            return {"uid": self.uid}

        def balance(self):
            calls.append("balance")
            return [{"details": [{"ccy": "USDT", "cashBal": "992"}, {"ccy": "BTC", "cashBal": "1.097"}]}]

        def ticker(self, product):
            calls.append("ticker")
            return {"bidPx": "100", "askPx": "101"}

    client = Client()
    monkeypatch.setattr(tool, "demo_client_from_env", lambda **kw: client)
    return tool, source, target, settings, client, calls


def test_migration_entrypoint_accounts_for_buys_sells_and_base_fees(migration):
    tool, source, target, settings, client, calls = migration
    before = audit.read_ledger(source)
    assert tool.main(["--target", str(target)]) == 0
    view = audit.read_ledger(target)
    assert D(view["meta"]["cash"]) == D("92")
    assert D(view["meta"]["positions"]["BTC-USDT"]) == D(".097")
    assert D(view["meta"]["anchor"]) == D("101.7")  # Mark at bid.
    assert {k: D(v) for k, v in view["meta"]["baseline"].items()} == {"USDT": D("1000"), "BTC": D("1")}
    assert view["meta"]["migration"]["state"] == "staged"
    assert view["meta"]["migration"]["build_complete"] is True
    assert view["meta"]["order_attempts"] == 2
    assert calls.count("balance") == 1
    assert audit.read_ledger(source) == before


@pytest.mark.parametrize("condition", ["unknown", "other_account", "locked", "wrong_credentials"])
def test_migration_refuses_unverifiable_sources(migration, condition):
    tool, source, target, settings, client, calls = migration
    lock = None
    if condition == "unknown":
        ledger = Ledger(source / "ledger.sqlite", settings, "okx-demo")
        plan = ledger.reserve(_plan(product="BTC-USDT"), time.time())
        ledger.begin_attempt(plan["client_order_id"])
        ledger.close()
    elif condition == "other_account":
        other = Ledger(source.parent / "other" / "ledger.sqlite", settings, "okx-demo")
        other.bind_account("different-account")
        other.put("baseline", {"USDT": "1000"})
        other.close()
    elif condition == "locked":
        lock = locking.acquire(source / "worker.lock")
    else:
        client.uid = "wrong-account"
    try:
        assert tool.main(["--target", str(target)]) == 1
        assert not (target / "ledger.sqlite").exists()
        assert calls == (["identity"] if condition == "wrong_credentials" else [])
    finally:
        if lock is not None:
            locking.release(lock)


def test_incomplete_migration_stays_unable_to_submit(migration, monkeypatch):
    tool, source, target, settings, client, calls = migration

    def fail_copy(*args):
        raise OSError("disk full")

    monkeypatch.setattr(tool, "copy_checkpoint", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        tool.main(["--target", str(target)])
    ledger = Ledger(target / "ledger.sqlite", settings, "okx-demo")
    try:
        assert ledger.get("migration") == {"state": "staged", "build_complete": False}
        with pytest.raises(MigrationNotPromoted):
            ledger.begin_attempt("anything")
    finally:
        ledger.close()


def test_migration_cannot_invent_a_missing_source_budget(migration):
    tool, source, target, settings, client, calls = migration
    ledger = Ledger(source / "ledger.sqlite", settings, "okx-demo")
    ledger.put("budget", None)
    ledger.close()
    assert tool.main(["--target", str(target)]) == 1
    assert "balance" not in calls
    assert not (target / "ledger.sqlite").exists()


def test_health_uses_new_failure_even_when_latest_json_is_success(tmp_path):
    from stonkfly.watch import read_health

    now = time.time()
    good = {"wall_time": now - 10, "neural": {"side": "HOLD"}}
    bad = {"wall_time": now, "type": "tick_skipped", "consecutive": 5}
    (tmp_path / "latest.json").write_text(json.dumps(good))
    (tmp_path / "events.jsonl").write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n")
    assert read_health(tmp_path)["network"] == "down"


def test_tail_detects_rotation_even_if_new_file_has_same_size(tmp_path):
    from stonkfly.watch import _Tail

    path = tmp_path / "events.jsonl"
    path.write_text('{"tick": 1}\n')
    tail = _Tail(path)
    path.replace(tmp_path / "events.1.jsonl")
    path.write_text('{"tick": 2}\n')
    assert tail.poll() == [{"tick": 2}]


def test_msvc_build_uses_native_flags_and_exports_the_kernel(tmp_path, monkeypatch):
    from stonkfly.neural import brain

    library = tmp_path / "cache" / "memory.dll"
    source = tmp_path / "kernel.cpp"
    source.write_text('extern "C" void memory_advance() {}')
    monkeypatch.setattr(brain, "LIBRARY", library)
    monkeypatch.setattr(brain, "SOURCE", source)
    monkeypatch.setattr(brain, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(brain.shutil, "which", lambda name: "cl.exe" if name == "cl" else None)
    calls = []

    def compile(command, check, cwd):
        assert check is True and cwd == library.parent
        calls.append(command)
        library.with_suffix(".dll.partial").write_bytes(b"test-only library")

    monkeypatch.setattr(brain.subprocess, "run", compile)
    brain.build()
    assert "/LD" in calls[0]
    assert "/EXPORT:memory_advance" in calls[0]
    assert "-O3" not in calls[0]
    assert library.exists()


def test_deployed_checkpoint_name_is_accepted_by_migration_and_restore(tmp_path):
    from stonkfly.neural.brain import checkpoint_path, restore_verified

    content = b"test state"
    digest = hashlib.sha256(content).hexdigest()
    name = f"brain-pretrained-{digest}.npz"
    (tmp_path / name).write_bytes(content)
    restored = []
    controller = SimpleNamespace(restore=restored.append)
    tool = _promotion_module()
    assert tool.checkpoint_path(tmp_path, name) == checkpoint_path(tmp_path, name)
    restore_verified(controller, tmp_path, {"file": name, "sha256": digest})
    assert restored == [tmp_path / name]


def test_rejected_ledger_open_closes_its_database(tmp_path, monkeypatch):
    import sqlite3
    from stonkfly import ledger as module

    path = tmp_path / "ledger.sqlite"
    Ledger(path, Settings(), "paper").close()
    opened = []
    closed = []
    real_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs, factory=TrackedConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    with pytest.raises(RuntimeError, match="mismatch"):
        Ledger(path, Settings(capital="50"), "paper")
    assert opened == closed


@pytest.mark.parametrize("change", ["missing", "negative", "side", "fee_currency"])
def test_audit_refuses_corrupt_settlements_instead_of_skipping_them(change):
    row = {"plan": {"product": "BTC-USDT", "side": "BUY"},
           "settlement": {"base": "1", "quote": "100", "fee": "0", "fee_ccy": "quote"}}
    if change == "missing":
        row["settlement"] = None
    elif change == "negative":
        row["settlement"]["base"] = "-1"
    elif change == "side":
        row["plan"]["side"] = "HOLD"
    else:
        row["settlement"]["fee_ccy"] = "unknown"
    with pytest.raises(ValueError):
        audit.bot_contribution([row], "USDT")


def test_paper_recovery_cannot_bypass_an_exhausted_attempt_budget(tmp_path):
    settings = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper")
    try:
        ledger.set_attempt_limit(1)
        ledger.put("order_attempts", 1)
        ledger.reserve(_plan(), time.time())
        with pytest.raises(AttemptLimitReached):
            PaperBroker(settings, ledger).reconcile()
        assert ledger.cash == D("100")
        assert ledger.positions == {}
        assert not ledger.pending()
        assert ledger.attempts_used() == 1
    finally:
        ledger.close()


def test_readers_keep_reserved_path_characters_inside_the_filename(tmp_path):
    from stonkfly.watch import read_meta, read_pending_count

    out = tmp_path / "run #1"
    Ledger(out / "ledger.sqlite", Settings(), "paper").close()
    assert audit.read_ledger(out)["meta"]["cash"] == "100"
    assert read_meta(out)["tick"] == 0
    assert read_pending_count(out) == 0
    assert list(tmp_path.iterdir()) == [out]
