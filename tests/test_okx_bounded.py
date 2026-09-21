"""Bounded-run tests: the persisted order-attempt budget.

Each test reproduces a way the budget could be evaded or silently reset, using
an in-memory exchange double that mirrors the fills it reports so the bot's
reconciliation stays consistent across several submissions. No socket is
opened and no credential is read.
"""

import json
import time

import pytest

from stonkfly.broker import UnresolvedOrder
from stonkfly.config import D, Settings
from stonkfly.ledger import AttemptLimitReached, Ledger
from stonkfly.market import Quote
from stonkfly.okx_broker import OKXBroker
from stonkfly.okx_client import OKXBusinessError, OKXClient, OKXTransportError
from stonkfly.risk import Guard, Veto

from okx_doubles import ACCOUNT_CONFIG, OrderQueryDoubles, detail, risk_snapshot


def quote(bid="100", ask="100.1"):
    return Quote(
        "BTC-USDT", D(bid), D(ask), time.time(),
        D(".00000001"), D(".01"), D(".01"), D("1"), D(".00000001"),
    )


class MirroringOKX(OrderQueryDoubles):
    """A demo account double whose balances follow the fills it reports."""

    def __init__(self):
        self.demo = True
        self.acct = dict(ACCOUNT_CONFIG)
        self.quote_bal = D("1000")  # includes the 100 granted to the bot
        self.base_bal = D("1")      # gifted base, never the bot's to sell
        self.orders = {}            # clOrdId -> order
        self.by_oid = {}
        self.submissions = []
        self.fail_submission = None  # exception or response dict for the next send
        self.final_state = "filled"
        self.open_orders = []
        self.untriggered_algos = []
        self.algo_pending_transient = ()
        self.archive_orders = []
        self.open_positions = []
        self.n = 0
        self.on_send = None         # hook run inside place_order

    def account_config(self):
        return self.acct

    def balance(self, ccys=None):
        return [{"details": [detail("USDT", self.quote_bal), detail("BTC", self.base_bal)]}]

    def positions(self, inst_type=None):
        return list(self.open_positions)

    def account_position_risk(self, inst_type):
        return risk_snapshot(posData=list(self.open_positions))

    def place_order(self, payload):
        if self.on_send is not None:
            self.on_send(payload)
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
        value = size * price
        if payload["side"] == "buy":
            self.quote_bal -= value
            self.base_bal += size
        else:
            self.quote_bal += value
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
        order = self.by_oid.get(ord_id) if ord_id else self.orders.get(cl_ord_id)
        if order is None:
            return None
        # The exchange's view of an order's state keeps evolving after it was
        # accepted, so report the current state rather than the one at submit.
        return {**order, "state": self.final_state}


@pytest.fixture
def env(tmp_path):
    s = Settings(products=("BTC-USDT",))
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "okx-demo")
    guard = Guard(s, ledger, tmp_path / "STOP")
    fake = MirroringOKX()
    broker = OKXBroker(s, ledger, fake)
    yield s, ledger, guard, fake, broker, tmp_path
    ledger.close()


def submit(env, side="BUY"):
    s, l, g, fake, broker, _ = env
    l.put("last_attempt", 0)  # the 60 s cooldown is not what these tests exercise
    plan = l.reserve(g.plan("BTC-USDT", side, {"BTC-USDT": quote()}), time.time())
    return plan, broker.execute(plan, g.before_submit)


# ---------------------------------------------------------------------------
# The budget is enforced at the send boundary and consumed before the request.
# ---------------------------------------------------------------------------

def test_third_submission_is_blocked(env):
    s, l, g, fake, broker, _ = env
    l.set_attempt_limit(2)
    broker.preflight()
    assert submit(env)[1]["status"] == "SETTLED"
    plan2, result2 = submit(env)
    assert result2["status"] == "SETTLED"
    assert l.attempts_used() == 2
    assert l.attempts_remaining() == 0
    assert len(fake.submissions) == 2
    # The guard no longer proposes a trade...
    l.put("last_attempt", 0)  # isolate the budget stop from the cooldown stop
    with pytest.raises(Veto, match="attempt budget"):
        g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()})
    # ...and an intent that already exists cannot slip past the send boundary.
    stale = {**plan2, "client_order_id": "a" * 32}
    l.db.execute(
        "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
        (stale["client_order_id"], "PREPARED", time.time(), json.dumps(stale)),
    )
    with pytest.raises(AttemptLimitReached):
        broker.execute(stale, g.before_submit)
    assert len(fake.submissions) == 2  # nothing reached the exchange
    assert not l.pending()             # the stale intent is abandoned, not unresolved


def test_attempt_is_recorded_before_the_request_leaves(env):
    s, l, g, fake, broker, _ = env
    l.set_attempt_limit(2)
    broker.preflight()
    seen = {}

    def capture(payload):
        # Inside the send: the attempt is already spent and the intent is
        # already UNKNOWN, so a crash here can only over-count.
        seen["attempts"] = l.attempts_used()
        seen["status"] = l.pending()[0]["status"]

    fake.on_send = capture
    submit(env)
    assert seen == {"attempts": 1, "status": "UNKNOWN"}


def test_a_vetoed_intent_does_not_spend_an_attempt(env):
    s, l, g, fake, broker, _ = env
    l.set_attempt_limit(1)
    broker.preflight()
    l.put("last_attempt", 0)
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    g.stop_file.touch()
    with pytest.raises(Veto):
        broker.execute(plan, g.before_submit)
    assert l.attempts_used() == 0  # never reached the boundary
    assert not l.pending()


# ---------------------------------------------------------------------------
# Restart: no resend, no reset.
# ---------------------------------------------------------------------------

def test_second_timeout_then_restart_does_not_resend_or_reset(env):
    s, l, g, fake, broker, tmp_path = env
    l.set_attempt_limit(2)
    broker.preflight()
    assert submit(env)[1]["status"] == "SETTLED"

    # The second submission's response is lost: the outcome is unknown.
    fake.fail_submission = OKXTransportError("OKX HTTP error", status=504)
    with pytest.raises(UnresolvedOrder):
        submit(env)
    assert l.attempts_used() == 2          # a lost response still spends an attempt
    assert l.attempts_remaining() == 0
    assert l.pending()[0]["status"] == "UNKNOWN"
    assert len(fake.submissions) == 2

    # Restart in the same directory: the budget and the unknown intent persist.
    fake.fail_submission = None
    fresh = Ledger(tmp_path / "ledger.sqlite", s, "okx-demo")
    broker2 = OKXBroker(s, fresh, fake)
    assert fresh.attempts_used() == 2
    assert fresh.attempts_remaining() == 0
    with pytest.raises(UnresolvedOrder):
        broker2.reconcile()                # cannot be found: no blind resubmission
    assert len(fake.submissions) == 2      # nothing was resent
    with pytest.raises(AttemptLimitReached):
        fresh.begin_attempt(fresh.pending()[0]["id"])
    fresh.close()


def test_reconcile_still_settles_after_the_budget_is_exhausted(env, monkeypatch):
    # The accept-to-terminal poll is capped at 30 s of wall clock; drive it with
    # a fake clock so the test does not have to wait for it.
    t = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(time, "sleep", lambda s: t.__setitem__(0, t[0] + s))
    s, l, g, fake, broker, tmp_path = env
    l.set_attempt_limit(1)
    broker.preflight()
    # Accepted but not yet terminal: the submission is spent, the fill is not.
    fake.final_state = "live"
    with pytest.raises(UnresolvedOrder):
        submit(env)
    assert l.attempts_remaining() == 0
    assert l.pending()[0]["status"] == "ACCEPTED"

    fake.final_state = "filled"
    fresh = Ledger(tmp_path / "ledger.sqlite", s, "okx-demo")
    broker2 = OKXBroker(s, fresh, fake)
    broker2.reconcile()
    assert not fresh.pending()
    assert fresh.positions["BTC-USDT"] > D(0)
    assert len(fake.submissions) == 1      # reconciliation never submits
    fresh.close()


def test_restart_in_the_same_directory_keeps_the_budget_bound(env):
    s, l, g, fake, broker, tmp_path = env
    l.set_attempt_limit(2)
    broker.preflight()
    fresh = Ledger(tmp_path / "ledger.sqlite", s, "okx-demo")
    fresh.set_attempt_limit(2)             # same value: resumes silently
    assert fresh.get("attempt_limit") == 2
    with pytest.raises(RuntimeError, match="allow-attempt-limit-change"):
        fresh.set_attempt_limit(5)
    fresh.set_attempt_limit(5, allow_change=True)
    assert fresh.get("attempt_limit") == 5
    assert fresh.attempts_used() == 0      # changing the bound never resets usage
    fresh.close()


def test_legacy_ledger_counts_existing_orders_as_spent_attempts(tmp_path):
    # A ledger written before the budget existed has no counter, but its rows
    # may already have reached the exchange, so they are counted, not zeroed.
    s = Settings(products=("BTC-USDT",))
    l = Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    for i in range(2):
        l.db.execute(
            "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
            (f"{i}" * 32, "SETTLED", time.time(), json.dumps({"product": "BTC-USDT"})),
        )
    l.db.execute("DELETE FROM meta WHERE key='order_attempts'")
    l.close()
    # Reopening seeds the counter from the rows that are already there.
    reopened = Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    assert reopened.attempts_used() == 2
    reopened.set_attempt_limit(2)
    assert reopened.attempts_remaining() == 0
    reopened.close()


def test_zero_limit_is_unbounded(env):
    s, l, g, fake, broker, _ = env
    l.set_attempt_limit(0)
    broker.preflight()
    assert l.attempts_remaining() is None
    assert submit(env)[1]["status"] == "SETTLED"
    assert l.attempts_remaining() is None


def test_negative_limit_rejected(env):
    _, l, _, _, _, _ = env
    with pytest.raises(ValueError):
        l.set_attempt_limit(-1)


# ---------------------------------------------------------------------------
# Gifted inventory stays untouched.
# ---------------------------------------------------------------------------

def test_selling_the_bots_whole_position_never_touches_gifted_base(env):
    s, l, g, fake, broker, _ = env
    l.set_attempt_limit(4)
    broker.preflight()
    gifted = fake.base_bal
    submit(env, "BUY")
    bought = l.positions["BTC-USDT"]
    assert bought > D(0)
    assert fake.base_bal == gifted + bought
    plan, result = submit(env, "SELL")
    assert result["status"] == "SETTLED"
    size = D(plan["base_size"])
    assert size < bought                       # a base-fee reserve is held back
    assert fake.base_bal > gifted              # gifted base never sold
    assert l.positions["BTC-USDT"] == bought - size


# ---------------------------------------------------------------------------
# Read-only queries retry within a bound; order placement never retries.
# ---------------------------------------------------------------------------

def test_read_only_query_retries_are_bounded_then_raise(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = []

    def transport(method, url, headers, data):
        calls.append(url)
        return 200, {"code": "51054", "msg": "Request timed out. Please try again.", "data": []}

    c = OKXClient(api_key="K", secret="S", passphrase="P", transport=transport)
    with pytest.raises(OKXBusinessError) as ei:
        c.orders_pending()
    assert len(calls) == 3        # bounded, not endless
    assert ei.value.code == "51054"


def test_read_only_query_succeeds_within_its_retry_budget(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = []

    def transport(method, url, headers, data):
        calls.append(url)
        if len(calls) < 3:
            return 200, {"code": "51054", "msg": "Request timed out", "data": []}
        return 200, {"code": "0", "data": []}

    assert OKXClient(api_key="K", secret="S", passphrase="P", transport=transport).orders_pending() == []
    assert len(calls) == 3


def test_place_order_is_never_retried(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = []

    def transport(method, url, headers, data):
        calls.append(url)
        return 200, {"code": "50013", "msg": "System busy", "data": []}

    OKXClient(api_key="K", secret="S", passphrase="P", transport=transport).place_order({"instId": "BTC-USDT"})
    assert len(calls) == 1        # an ambiguous send is never repeated


def test_read_only_queries_only_issue_get_requests():
    methods = []

    def transport(method, url, headers, data):
        methods.append(method)
        return 200, {"code": "0", "data": []}

    c = OKXClient(api_key="K", secret="S", passphrase="P", transport=transport)
    c.orders_pending()
    c.orders_algo_pending("trigger")
    assert methods == ["GET", "GET"]


def test_send_boundary_does_not_repeat_the_full_algo_sweep(env):
    # Every OKX request costs about a second and the plan must reach the exchange
    # inside the quote-age window, so the boundary checks the algo types OKX
    # actually answers and leaves the full sweep to the tick. Repeating the full
    # sweep here would age the plan past the window and veto every real proposal.
    s, l, g, fake, broker, _ = env
    l.set_attempt_limit(2)
    broker.preflight()
    asked = []

    def counting(ord_type, retries=3):
        asked.append(ord_type)
        return []

    fake.orders_algo_pending = counting
    plan, result = submit(env)
    assert result["status"] == "SETTLED"
    assert asked == ["conditional,oco"]
    # The order really was sent, so the boundary gate was passed, not skipped.
    assert len(fake.submissions) == 1
    assert l.attempts_used() == 1


def test_provenance_is_stable_across_attempts(tmp_path):
    # The recorded provenance identifies the run protocol, so it must not change
    # as attempts are consumed -- otherwise restarting a directory that has
    # already traded reports "source changed" and refuses to resume.
    from stonkfly.cli import _attempt_budget_record

    l = Ledger(tmp_path / "l.sqlite", Settings(products=("BTC-USDT",)), "okx-demo")
    l.set_attempt_limit(2)
    before = _attempt_budget_record(l)
    l.put("order_attempts", 2)
    after = _attempt_budget_record(l)
    assert before == after == {"limit": 2}
    # The spent count is still observable, just not part of the hashed protocol.
    assert l.attempts_used() == 2
    l.close()
