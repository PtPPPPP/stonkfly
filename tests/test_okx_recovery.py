"""Recovery of an UNKNOWN submission, and the evidence needed to close one.

An ambiguous submission stops the worker by design. These tests pin down what a
human adjudication must prove before an intent may be closed, and that nothing
else in the system is allowed to close one. Every exchange call is an in-memory
double; no network order is placed.
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
from stonkfly.okx_client import OKXBusinessError, OKXTransportError
from stonkfly.risk import Guard, Veto

from okx_doubles import ACCOUNT_CONFIG, OrderQueryDoubles, detail, risk_snapshot


def quote(bid="100", ask="100.1"):
    return Quote(
        "BTC-USDT", D(bid), D(ask), time.time(),
        D(".00000001"), D(".01"), D(".01"), D("1"), D(".00000001"),
    )


class MirroringOKX(OrderQueryDoubles):
    """Demo account double whose balances follow the fills it reports."""

    def __init__(self):
        self.demo = True
        self.acct = dict(ACCOUNT_CONFIG)
        self.quote_bal = D("1000")
        self.base_bal = D("1")
        self.orders = {}
        self.by_oid = {}
        self.submissions = []
        self.fail_submission = None
        self.final_state = "filled"
        self.open_orders = []
        self.open_positions = []
        self.untriggered_algos = []
        self.algo_pending_transient = ()
        self.history_orders = []
        self.archive_orders = []
        self.history_complete = True
        self.archive_complete = True
        self.lookups = []
        self.n = 0

    def account_config(self):
        return self.acct

    def balance(self, ccys=None):
        return [{"details": [detail("USDT", self.quote_bal), detail("BTC", self.base_bal)]}]

    def positions(self, inst_type=None):
        return list(self.open_positions)

    def account_position_risk(self, inst_type):
        return risk_snapshot(posData=list(self.open_positions))

    def place_order(self, payload):
        self.submissions.append(payload)
        fail = self.fail_submission
        if isinstance(fail, Exception):
            raise fail
        if fail is not None:
            return fail
        self.n += 1
        oid = f"ord-{self.n}"
        size = D(payload["sz"])
        price = D(payload["px"])
        if payload["side"] == "buy":
            self.quote_bal -= size * price
            self.base_bal += size
        else:
            self.quote_bal += size * price
            self.base_bal -= size
        order = {
            "ordId": oid, "clOrdId": payload["clOrdId"], "instId": payload["instId"],
            "side": payload["side"], "state": self.final_state,
            "accFillSz": payload["sz"], "avgPx": payload["px"],
            "fee": "0", "feeCcy": "", "rebate": "", "rebateCcy": "",
        }
        self.orders[payload["clOrdId"]] = order
        self.by_oid[oid] = order
        return {"code": "0", "data": [{"sCode": "0", "ordId": oid, "clOrdId": payload["clOrdId"]}]}

    def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        # OKX resolves an order *inside its instrument*: a lookup whose instId is
        # not a plain product string is rejected as an invalid symbol (51001,
        # observed live when the bound investigation forwarded a plan dict). The
        # double therefore rejects it too -- ignoring instId outright is what let
        # that defect reach a real run.
        if not isinstance(inst_id, str) or "-" not in inst_id:
            raise OKXBusinessError("order query: OKX code=51001", code="51001")
        self.lookups.append(inst_id)
        order = self.by_oid.get(ord_id) if ord_id else self.orders.get(cl_ord_id)
        return None if order is None else {**order, "state": self.final_state}


@pytest.fixture
def env(tmp_path):
    s = Settings(products=("BTC-USDT",))
    l = Ledger(tmp_path / "ledger.sqlite", s, "okx-demo")
    g = Guard(s, l, tmp_path / "STOP")
    fake = MirroringOKX()
    broker = OKXBroker(s, l, fake)
    yield s, l, g, fake, broker
    l.close()


def _unknown_intent(env):
    """Spend one attempt whose response is lost, leaving the intent UNKNOWN."""
    s, l, g, fake, broker = env
    l.set_attempt_limit(2)
    broker.preflight()
    l.put("last_attempt", 0)
    fake.fail_submission = OKXTransportError("OKX HTTP error", status=504)
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    fake.fail_submission = None
    row = l.pending()[0]
    assert row["status"] == "UNKNOWN" and row["exchange_id"] is None
    return row["id"]


def _as_if_existing(cid):
    return {
        "ordId": "ord-x", "clOrdId": cid, "instId": "BTC-USDT", "side": "buy",
        "state": "filled", "accFillSz": "0.0001", "avgPx": "76000",
        "fee": "0", "feeCcy": "", "rebate": "", "rebateCcy": "",
    }


# ---------------------------------------------------------------------------
# Nothing except an adjudication may close an UNKNOWN intent.
# ---------------------------------------------------------------------------

def test_absence_alone_never_closes_an_unknown(env):
    # A restart, reconcile and --resume-reviewed all leave the intent open: a
    # query that finds nothing is not evidence that no order exists.
    s, l, g, fake, broker = env
    _unknown_intent(env)
    with pytest.raises(UnresolvedOrder):
        broker.reconcile()
    assert l.pending()[0]["status"] == "UNKNOWN"
    assert l.get("resolved_orders") is None
    # And the guard still refuses to let a new proposal through.
    l.put("last_attempt", 0)
    with pytest.raises(Veto, match="unresolved"):
        g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()})


def test_unknown_blocks_preflight_and_continuous_start(env):
    s, l, g, fake, broker = env
    _unknown_intent(env)
    with pytest.raises(UnresolvedOrder):
        broker.preflight()


def test_a_prepared_intent_is_still_a_definite_not_sent(env):
    # PREPARED cannot have been sent (the durable UNKNOWN transition precedes any
    # request), so reconcile may close it without any evidence gathering.
    s, l, g, fake, broker = env
    broker.preflight()
    l.set_attempt_limit(2)
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    assert l.pending()[0]["status"] == "PREPARED"
    broker.reconcile()
    assert not l.pending()
    assert fake.submissions == []


# ---------------------------------------------------------------------------
# Investigation is read-only and says what it did and did not establish.
# ---------------------------------------------------------------------------

def test_investigation_is_read_only(env):
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    row = l.pending()[0]
    before = (l.get("cash"), l.get("positions"), l.get("order_attempts"))
    findings = broker.investigate_unknown(cid, row["plan"], row["created"])
    assert findings["lookup"] is None
    assert findings["history_complete"] and findings["archive_complete"]
    assert findings["history_rows"] == 0 and findings["archive_rows"] == 0
    assert findings["age_seconds"] > 0
    # Nothing moved, and nothing was concluded.
    assert (l.get("cash"), l.get("positions"), l.get("order_attempts")) == before
    assert l.pending()[0]["status"] == "UNKNOWN"
    assert l.get("resolved_orders") is None


def test_investigation_looks_the_intent_up_inside_its_product(env):
    """The order lookup must carry the product string, never the plan dict.

    The investigation hands this value straight to ``instId``. Passing the whole
    plan made OKX answer 51001 (invalid symbol), which broke the only documented
    way to close an UNKNOWN intent -- while the audit tool, which passes the
    product itself, kept working.
    """
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    row = l.pending()[0]
    findings = broker.investigate_unknown(cid, row["plan"], row["created"])
    assert fake.lookups == ["BTC-USDT"]
    assert findings["lookup"] is None


# ---------------------------------------------------------------------------
# Adjudication: named, proven, atomic, and recorded as a human judgement.
# ---------------------------------------------------------------------------

def test_adjudication_closes_the_named_intent_and_records_its_basis(env):
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    record = broker.adjudicate_absent(cid)
    assert record["id"] == cid
    assert record["reason"] == "adjudicated_absent"
    assert record["source"] == "operator"
    # The record states its own epistemic status: a judgement, not confirmation.
    assert record["confirmation"] == "human_adjudication_not_exchange_confirmation"
    assert "no match" in record["basis"]
    assert not l.pending()
    assert l.cash == D("100") and l.positions == {}
    # The spent attempt is never handed back.
    assert l.attempts_used() == 1


def test_a_closed_intent_is_never_rewritten(env):
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    record = broker.adjudicate_absent(cid)
    trail = list(l.get("resolved_orders"))
    # The intent is gone from the pending set, so a second call cannot touch it
    # and cannot append a second audit entry.
    with pytest.raises(RuntimeError):
        broker.adjudicate_absent(cid)
    assert l.get("resolved_orders") == trail == [record]


def test_adjudication_refuses_an_unknown_intent_it_was_not_named_for(env):
    s, l, g, fake, broker = env
    _unknown_intent(env)
    with pytest.raises(RuntimeError, match="No uncertain intent"):
        broker.adjudicate_absent("f" * 32)


@pytest.mark.parametrize("where", ["lookup", "open_orders", "history", "archive"])
def test_a_visible_order_cannot_be_adjudicated_absent(env, where):
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    target = {
        "open_orders": "open_orders",
        "history": "history_orders",
        "archive": "archive_orders",
    }
    if where == "lookup":
        fake.orders[cid] = _as_if_existing(cid)
    else:
        setattr(fake, target[where], [_as_if_existing(cid)])
    with pytest.raises(RuntimeError, match="visible"):
        broker.adjudicate_absent(cid)
    assert l.pending()[0]["status"] == "UNKNOWN"   # left exactly as it was
    assert l.get("resolved_orders") is None


@pytest.mark.parametrize("flag", ["history_complete", "archive_complete"])
def test_incomplete_scan_cannot_close_an_unknown(env, flag):
    # A truncated scan is not evidence of absence, so it must refuse.
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    setattr(fake, flag, False)
    with pytest.raises(RuntimeError, match="did not complete"):
        broker.adjudicate_absent(cid)
    assert l.pending()[0]["status"] == "UNKNOWN"


def test_balances_must_reconcile_before_any_state_change(env):
    # If the "absent" order had actually moved funds, reconciliation catches it
    # and the intent stays UNKNOWN rather than closing over a real fill.
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    fake.quote_bal -= D("10")
    with pytest.raises(RuntimeError, match="External balance change"):
        broker.adjudicate_absent(cid)
    assert l.pending()[0]["status"] == "UNKNOWN"
    assert l.get("resolved_orders") is None


def test_identity_must_still_match_before_any_state_change(env):
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    fake.acct.update(uid="uid-2", mainUid="uid-2")
    with pytest.raises(RuntimeError, match="mismatch"):
        broker.adjudicate_absent(cid)
    assert l.pending()[0]["status"] == "UNKNOWN"


def test_adjudication_requires_a_fill_or_kill_intent(env):
    # Only an all-or-nothing order can be reasoned about this way: a partially
    # fillable order would leave "no full fill" ambiguous.
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    row = l.pending()[0]
    l.db.execute(
        "UPDATE orders SET plan=? WHERE id=?",
        (json.dumps({**row["plan"], "order_type": "limit_limit_gtc"}), cid),
    )
    with pytest.raises(RuntimeError, match="fill-or-kill"):
        broker.adjudicate_absent(cid)
    assert l.pending()[0]["status"] == "UNKNOWN"


def test_adjudication_rolls_back_when_the_commit_step_fails(env, monkeypatch):
    # Status change and audit record are one transaction: if either fails, the
    # intent stays UNKNOWN with no audit entry.
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    class FlakyConnection:
        """Forwards everything, but fails the status write."""

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def execute(self, sql, *args):
            if sql.startswith("UPDATE orders SET status='REJECTED'"):
                raise sqlite3.OperationalError("simulated failure")
            return self._real.execute(sql, *args)

    monkeypatch.setattr(l, "db", FlakyConnection(l.db))
    with pytest.raises(sqlite3.OperationalError):
        broker.adjudicate_absent(cid)
    monkeypatch.undo()
    assert l.pending()[0]["status"] == "UNKNOWN"
    assert l.get("resolved_orders") is None


# ---------------------------------------------------------------------------
# The retention window decides what a clean miss is worth, and the basis says so.
# ---------------------------------------------------------------------------

def test_fresh_intent_records_the_stronger_window(env):
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    record = broker.adjudicate_absent(cid)
    assert "age <= 2h" in record["basis"]
    assert "canceled-without-fill order would still be listed" in record["basis"]


def test_stale_intent_records_the_weaker_window(env):
    # Past two hours a canceled-without-fill order is no longer listed at all, so
    # the basis must not claim the stronger result.
    s, l, g, fake, broker = env
    cid = _unknown_intent(env)
    row = l.pending()[0]
    old = row["created"] - 9000
    l.db.execute("UPDATE orders SET created=? WHERE id=?", (old, cid))
    findings = broker.investigate_unknown(cid, row["plan"], old)
    ok, basis = broker.adjudication_basis(row["plan"], old, findings)
    assert ok is True
    assert "age > 2h" in basis
    assert "canceled-without-fill order is no longer listed" in basis


def test_a_rejection_records_the_exchange_message(env):
    # The sMsg template is what names the exact reason -- for the sell failures
    # it carried the price-limit bound of 51138. It is fixed exchange vocabulary.
    s, l, g, fake, broker = env
    l.set_attempt_limit(2)
    broker.preflight()
    l.put("last_attempt", 0)
    fake.fail_submission = {
        "code": "1",
        "data": [{
            "sCode": "51138",
            "sMsg": "The lowest price limit for sell orders is 80,573.2.",
        }],
    }
    l.put("positions", {"BTC-USDT": "0.2"})
    l.put("cash", "80")
    fake.quote_bal = D("980")
    fake.base_bal = D("1.2")
    plan = l.reserve(g.plan("BTC-USDT", "SELL", {"BTC-USDT": quote()}), time.time())
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "REJECTED"
    assert result["order_scode"] == "51138"
    assert result["order_smsg"] == "The lowest price limit for sell orders is 80,573.2."
    assert not l.pending()
