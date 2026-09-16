"""Hardening counter-example tests for the OKX adapter (issues 1-6).

Each test reproduces a specific defect in the current adapter; several are
written to FAIL against the buggy code and PASS after the fix. Every exchange
call is an in-memory double; no network or credentials are used.
"""

import json
import sqlite3
import time

import pytest

from stonkfly.broker import UnresolvedOrder
from stonkfly.config import D, Settings
from stonkfly.ledger import Ledger
from stonkfly.market import Quote
from stonkfly.okx_broker import OKXBroker
from stonkfly.okx_client import OKXBusinessError, OKXClient
from stonkfly.okx_market import OKXMarket
from stonkfly.risk import Guard


def quote():
    return Quote(
        "BTC-USDC", D("100"), D("100.1"), time.time(),
        D(".00000001"), D(".01"), D(".01"), D("1"), D(".00000001"),
    )


# ---------------------------------------------------------------------------
# Broker double (in-memory; demo by default)
# ---------------------------------------------------------------------------

class FakeBrokerClient:
    def __init__(self, demo=True):
        self.demo = demo
        self.acct = {"acctLv": "1", "perm": "read,trade", "uid": "uid-1"}
        self.balances = {"USDC": D("100"), "BTC": D("0")}
        self.submissions = []
        self.place_response = {"code": "0", "data": [{"sCode": "0", "ordId": "ord-1"}]}
        self.order = None  # what get_order returns
        self.open_orders = []  # what orders_pending returns

    def account_config(self):
        return self.acct

    def balance(self, ccys):
        return [{
            "details": [
                {"ccy": c, "availBal": str(self.balances.get(c, D("0"))),
                 "frozenBal": "0", "ordFrozen": "0"}
                for c in ccys
            ]
        }]

    def place_order(self, payload):
        self.submissions.append(payload)
        r = self.place_response
        if isinstance(r, dict) and isinstance(r.get("data"), list) and r["data"]:
            item = r["data"][0]
            if isinstance(item, dict):
                r = dict(r)
                r["data"] = [dict(item, clOrdId=payload["clOrdId"])]
        return r

    def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        return self.order

    def orders_pending(self, inst_id):
        return self.open_orders


@pytest.fixture
def env(tmp_path):
    s = Settings()
    l = Ledger(tmp_path / "ledger.sqlite", s, "okx-demo")
    g = Guard(s, l, tmp_path / "STOP")
    fake = FakeBrokerClient()
    broker = OKXBroker(s, l, fake)
    yield s, l, g, fake, broker
    l.close()


# ---------------------------------------------------------------------------
# Issue 1: preflight verifies account identity BEFORE reconcile.
# ---------------------------------------------------------------------------

def test_preflight_identity_checked_before_reconcile(env):
    s, l, g, fake, broker = env
    broker.preflight()  # binds uid-1
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    assert l.pending()[0]["status"] == "PREPARED"
    # Reconnect with a different account identity.
    fake.acct["uid"] = "uid-2"
    with pytest.raises(RuntimeError, match="mismatch"):
        OKXBroker(s, l, fake).preflight()
    # reconcile must NOT have run: the intent and balances are untouched.
    assert l.pending()[0]["status"] == "PREPARED"
    assert l.cash == D("100")
    assert fake.submissions == []


# ---------------------------------------------------------------------------
# Issue 2: ambiguous / malformed order responses keep UNKNOWN, never REJECTED.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("resp", [
    None,                                            # missing envelope
    {},                                              # no code
    {"msg": "x"},                                    # no code
    {"code": "1", "msg": "Operation failed", "data": []},   # ambiguous
    {"code": "50004", "msg": "timeout", "data": []},         # timeout (ambiguous)
    {"code": "50013", "msg": "busy", "data": []},            # system busy
    {"code": "50026", "msg": "system error", "data": []},    # system error
    {"code": "0", "data": []},                        # missing item
    {"code": "0", "data": [{"ordId": "x"}]},          # missing sCode
])
def test_ambiguous_order_response_stays_unknown(env, resp):
    s, l, g, fake, broker = env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    fake.place_response = resp
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert len(fake.submissions) == 1
    assert l.pending()[0]["status"] == "UNKNOWN"
    assert l.cash == D("100")


def test_definite_order_rejection_is_rejected(env):
    s, l, g, fake, broker = env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    fake.place_response = {"code": "0", "data": [{"sCode": "51008", "sMsg": "no funds", "ordId": ""}]}
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "REJECTED"
    assert not l.pending()
    assert len(fake.submissions) == 1


# ---------------------------------------------------------------------------
# Issue 3: missing fill/fee fields are not defaulted to zero.
# ---------------------------------------------------------------------------

def test_missing_fill_size_not_settled_as_zero(env):
    s, l, g, fake, broker = env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    fake.place_response = {"code": "0", "data": [{"sCode": "0", "ordId": "ord-1", "clOrdId": plan["client_order_id"]}]}
    fake.order = {  # terminal "filled" but accFillSz MISSING
        "ordId": "ord-1", "clOrdId": plan["client_order_id"], "instId": "BTC-USDC",
        "side": "buy", "state": "filled", "avgPx": "100.1",
        "fee": "-0.05", "feeCcy": "USDC", "rebate": "0", "rebateCcy": "",
    }
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert l.pending()[0]["status"] == "ACCEPTED"  # not settled
    assert l.cash == D("100")
    assert l.positions == {}


def test_missing_fee_not_settled_as_zero(env):
    s, l, g, fake, broker = env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    fake.place_response = {"code": "0", "data": [{"sCode": "0", "ordId": "ord-1", "clOrdId": plan["client_order_id"]}]}
    fake.order = {  # filled but fee MISSING
        "ordId": "ord-1", "clOrdId": plan["client_order_id"], "instId": "BTC-USDC",
        "side": "buy", "state": "filled", "accFillSz": plan["base_size"], "avgPx": "100.1",
        "rebate": "0", "rebateCcy": "",
    }
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert l.pending()[0]["status"] == "ACCEPTED"
    assert l.cash == D("100")


def test_legitimate_zero_fill_settles(env):
    s, l, g, fake, broker = env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    fake.place_response = {"code": "0", "data": [{"sCode": "0", "ordId": "ord-1", "clOrdId": plan["client_order_id"]}]}
    fake.order = {  # terminal cancel with explicit zero fill
        "ordId": "ord-1", "clOrdId": plan["client_order_id"], "instId": "BTC-USDC",
        "side": "buy", "state": "canceled", "accFillSz": "0", "avgPx": "",
        "fee": "0", "feeCcy": "", "rebate": "", "rebateCcy": "",
    }
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "SETTLED"
    assert not l.pending()
    assert l.cash == D("100")


def test_legitimate_zero_fee_on_fill_settles(env):
    s, l, g, fake, broker = env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    fake.place_response = {"code": "0", "data": [{"sCode": "0", "ordId": "ord-1", "clOrdId": plan["client_order_id"]}]}
    fake.order = {
        "ordId": "ord-1", "clOrdId": plan["client_order_id"], "instId": "BTC-USDC",
        "side": "buy", "state": "filled", "accFillSz": plan["base_size"], "avgPx": "100.1",
        "fee": "0", "feeCcy": "", "rebate": "", "rebateCcy": "",
    }
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "SETTLED"
    value = D(plan["base_size"]) * D("100.1")
    assert l.cash == D("100") - value


# ---------------------------------------------------------------------------
# Issue 4: every query validates the business envelope.
# ---------------------------------------------------------------------------

def _client_with(resp):
    def transport(method, url, headers, data):
        return 200, resp
    return OKXClient(api_key="K", secret="S", passphrase="P", demo=True, transport=transport)


def test_orders_pending_query_error_raises():
    c = _client_with({"code": "50013", "msg": "busy", "data": []})
    with pytest.raises(OKXBusinessError):
        c.orders_pending("BTC-USDC")


def test_balance_query_error_raises():
    c = _client_with({"code": "50013", "msg": "busy", "data": []})
    with pytest.raises(OKXBusinessError):
        c.balance(["USDC"])


def test_account_config_query_error_raises():
    c = _client_with({"code": "50013", "msg": "busy", "data": []})
    with pytest.raises(OKXBusinessError):
        c.account_config()


def test_get_order_not_found_returns_none():
    c = _client_with({"code": "51603", "msg": "Order does not exist.", "data": []})
    assert c.get_order("BTC-USDC", ord_id="x") is None


def test_get_order_query_error_raises():
    c = _client_with({"code": "50013", "msg": "busy", "data": []})
    with pytest.raises(OKXBusinessError):
        c.get_order("BTC-USDC", ord_id="x")


def test_verify_balances_propagates_open_order_query_failure(env, monkeypatch):
    s, l, g, fake, broker = env
    broker.preflight()

    def boom(inst_id):
        raise OKXBusinessError("open order query failed")

    monkeypatch.setattr(fake, "orders_pending", boom)
    with pytest.raises(OKXBusinessError):
        broker.verify_balances()


# ---------------------------------------------------------------------------
# Issue 5: quote_increment must not reuse the price tick.
# ---------------------------------------------------------------------------

class FakeMarketClient:
    def __init__(self):
        self.inst = {
            "instId": "BTC-USDC", "instType": "SPOT", "baseCcy": "BTC",
            "quoteCcy": "USDC", "state": "live", "lotSz": "0.00001",
            "tickSz": "0.1", "minSz": "0.0001",
        }
        self.tk = {"instId": "BTC-USDC", "bidPx": "100", "askPx": "100.1", "ts": "1700000000000"}
        now = int(time.time() * 1000)
        self.candles_data = [
            [str(now - 60000), "1", "1", "1", "101", "1", "1", "1", "1"],
            [str(now - 120000), "1", "1", "1", "99", "1", "1", "1", "1"],
        ]

    def instruments(self, inst_id):
        return self.inst

    def ticker(self, inst_id):
        return self.tk

    def candles(self, inst_id, bar="1m", limit=120):
        return self.candles_data


def test_quote_increment_and_minimum_quote_are_none():
    m = OKXMarket(("BTC-USDC",), FakeMarketClient())
    q = m.snapshot()["BTC-USDC"]
    assert q.price_increment == D("0.1")     # tickSz
    assert q.base_increment == D("0.00001")  # lotSz
    assert q.minimum_base == D("0.0001")     # minSz
    assert q.minimum_quote is None           # no quote minimum on OKX spot
    assert q.quote_increment is None         # no quote-amount step on OKX spot


def test_minimum_sell_not_rejected_by_fabricated_quote_minimum(tmp_path):
    # A minimum-size sell (position == minSz) after slippage produces
    # size * limit = minSz * bid * (1 - slippage) < minSz * bid, which a
    # fabricated minimum_quote of minSz x bid would wrongly veto. With
    # minimum_quote = None the guard enforces only the real base-minimum rule.
    s = Settings()
    l = Ledger(tmp_path / "l.sqlite", s, "paper")
    g = Guard(s, l, tmp_path / "STOP")
    l.put("positions", {"BTC-USDC": "0.0001"})  # exactly minSz
    q = Quote(
        "BTC-USDC", D("100"), D("100.1"), time.time(),
        D("0.00001"), None, D("0.1"), None, D("0.0001"),
    )
    plan = g.plan("BTC-USDC", "SELL", {"BTC-USDC": q})
    assert D(plan["base_size"]) == D("0.0001")
    l.close()


# ---------------------------------------------------------------------------
# Issue 6: demo-only client; legacy ledger identity must not be auto-adopted.
# ---------------------------------------------------------------------------

def test_non_demo_client_rejected(tmp_path):
    s = Settings()
    l = Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    with pytest.raises(RuntimeError, match="demo"):
        OKXBroker(s, l, OKXClient(api_key="K", secret="S", passphrase="P", demo=False))
    l.close()


def test_legacy_ledger_without_identity_rejected(tmp_path):
    # A ledger initialized before identity was recorded (identity key removed)
    # must not be silently adopted; reopening rejects it.
    s = Settings()
    l = Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    l.db.execute("DELETE FROM meta WHERE key='identity'")
    l.close()
    with pytest.raises(RuntimeError, match="identity"):
        Ledger(tmp_path / "l.sqlite", s, "okx-demo")


def test_identity_missing_with_activity_rejects_recovery_and_unchanged(tmp_path):
    # A demo ledger with historical activity but no recorded identity must not
    # auto-backfill identity, bind the current account, or start reconciling.
    s = Settings()
    l = Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    g = Guard(s, l, tmp_path / "STOP")
    fake = FakeBrokerClient()
    broker = OKXBroker(s, l, fake)
    broker.preflight()  # binds uid-1, demo_initialized
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    assert l.pending()[0]["status"] == "PREPARED"
    # Simulate a legacy/corrupt state: identity lost but activity present.
    l.db.execute("DELETE FROM meta WHERE key='identity'")
    l.close()
    # Reopening must reject and leave the ledger untouched.
    with pytest.raises(RuntimeError, match="identity"):
        Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    db = sqlite3.connect(tmp_path / "l.sqlite")
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
    n_orders = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    status = db.execute("SELECT status FROM orders").fetchone()[0]
    db.close()
    assert "identity" not in meta          # identity not backfilled
    assert n_orders == 1                    # order untouched
    assert status == "PREPARED"             # not reconciled / not rejected
    assert meta["cash"] == "100"            # balances unchanged
