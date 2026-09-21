"""OKX adapter tests. Every exchange call here is an in-memory double; no socket
is ever opened and no real OKX (live or demo) request leaves the process."""

import base64
import hashlib
import hmac
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from stonkfly.broker import UnresolvedOrder
from stonkfly.config import D, Settings
from stonkfly.ledger import Ledger
from stonkfly.market import Quote
from stonkfly.okx_broker import OKXBroker
from stonkfly.okx_client import OKXClient, OKXTransportError, USER_AGENT
from stonkfly.okx_market import OKXMarket
from stonkfly.risk import Guard, Veto

from okx_doubles import ACCOUNT_CONFIG, OrderQueryDoubles, detail as _detail, risk_snapshot


def quote():
    return Quote(
        "BTC-USDT", D("100"), D("100.1"), time.time(),
        D(".00000001"), D(".01"), D(".01"), D("1"), D(".00000001"),
    )


# ---------------------------------------------------------------------------
# OKXClient: signing, transport ambiguity, credential isolation
# ---------------------------------------------------------------------------

class CaptureTransport:
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data if data is not None else {"code": "0", "data": []}
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, dict(headers), payload))
        return self.status, self.data


def _sign(secret, message):
    return base64.b64encode(
        hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()


def test_signature_matches_request_and_includes_body():
    fixed = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t = CaptureTransport()
    client = OKXClient(
        api_key="KEY", secret="SECRET", passphrase="PASS",
        transport=t, clock=lambda: fixed,
    )
    client.request("POST", "/api/v5/trade/order", body={"instId": "BTC-USDC"}, auth=True)
    method, url, headers, payload = t.calls[0]
    ts = "2026-01-01T00:00:00.000Z"
    message = f"{ts}POST/api/v5/trade/order" + '{"instId":"BTC-USDC"}'
    assert headers["OK-ACCESS-KEY"] == "KEY"
    assert headers["OK-ACCESS-PASSPHRASE"] == "PASS"
    assert headers["OK-ACCESS-TIMESTAMP"] == ts
    assert headers["OK-ACCESS-SIGN"] == _sign("SECRET", message)
    assert "x-simulated-trading" not in headers  # demo not requested


def test_signature_includes_query_string():
    fixed = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t = CaptureTransport()
    client = OKXClient(
        api_key="K", secret="S", passphrase="P", transport=t, clock=lambda: fixed
    )
    client.request("GET", "/api/v5/trade/order", params={"instId": "B"}, auth=True)
    method, url, headers, payload = t.calls[0]
    ts = "2026-01-01T00:00:00.000Z"
    assert headers["OK-ACCESS-SIGN"] == _sign("S", f"{ts}GET/api/v5/trade/order?instId=B")
    assert "instId=B" in url


def test_demo_flag_adds_simulated_header():
    t = CaptureTransport()
    client = OKXClient(api_key="K", secret="S", passphrase="P", demo=True, transport=t)
    client.request("GET", "/api/v5/account/config", auth=True)
    assert t.calls[0][2]["x-simulated-trading"] == "1"


def test_public_request_carries_no_credentials():
    t = CaptureTransport()
    client = OKXClient(api_key="K", secret="S", passphrase="P", demo=True, transport=t)
    client.request("GET", "/api/v5/public/time")
    headers = t.calls[0][2]
    assert "OK-ACCESS-KEY" not in headers
    assert "OK-ACCESS-SIGN" not in headers
    assert "x-simulated-trading" not in headers


def test_non_200_is_transport_error():
    client = OKXClient(transport=CaptureTransport(status=429))
    with pytest.raises(OKXTransportError):
        client.request("GET", "/api/v5/public/time")


def test_connection_loss_is_transport_error():
    def boom(*a):
        raise OSError("connection reset")

    with pytest.raises(OKXTransportError):
        OKXClient(transport=boom).request("GET", "/api/v5/public/time")


def test_non_200_carries_http_status():
    client = OKXClient(transport=CaptureTransport(status=502))
    with pytest.raises(OKXTransportError) as ei:
        client.request("GET", "/api/v5/public/time")
    assert ei.value.status == 502


def test_connection_loss_preserves_cause_chain():
    def boom(*a):
        raise OSError("connection reset")

    with pytest.raises(OKXTransportError) as ei:
        OKXClient(transport=boom).request("GET", "/api/v5/public/time")
    assert ei.value.status is None
    assert isinstance(ei.value.__cause__, OSError)


def test_transport_failures_preserve_status_and_cause():
    # The transport's failures surface as OKXTransportError: a non-200
    # response carries its HTTP status so a diagnostic can tell a 502 from a
    # connection reset, and a connection-level failure preserves the cause.
    client = OKXClient(transport=lambda *a: (502, None))
    with pytest.raises(OKXTransportError) as ei:
        client.request("GET", "/api/v5/public/time")
    assert ei.value.status == 502

    def boom(*a):
        raise OSError("connection reset")

    client = OKXClient(transport=boom)
    with pytest.raises(OKXTransportError) as ei:
        client.request("GET", "/api/v5/public/time")
    assert ei.value.status is None
    assert isinstance(ei.value.__cause__, OSError)


def test_public_client_defaults_to_no_key():
    client = OKXClient()
    assert client.api_key is None and client.secret is None


def test_product_user_agent_on_public_request():
    t = CaptureTransport()
    client = OKXClient(transport=t)
    client.request("GET", "/api/v5/public/time")
    assert t.calls[0][2]["User-Agent"] == USER_AGENT == "stonkfly/0.1.0"


def test_product_user_agent_on_private_demo_request():
    t = CaptureTransport()
    client = OKXClient(api_key="K", secret="S", passphrase="P", demo=True, transport=t)
    client.request("GET", "/api/v5/account/config", auth=True)
    headers = t.calls[0][2]
    # product identity present, and signing + simulated flag untouched
    assert headers["User-Agent"] == USER_AGENT
    assert headers["OK-ACCESS-KEY"] == "K"
    assert headers["OK-ACCESS-SIGN"]
    assert headers["x-simulated-trading"] == "1"


# ---------------------------------------------------------------------------
# OKXMarket: instruments, precision, completed candles, abnormal data
# ---------------------------------------------------------------------------

class FakeOKXMarketClient:
    def __init__(self):
        self.inst = {
            "instId": "BTC-USDC", "instType": "SPOT", "baseCcy": "BTC",
            "quoteCcy": "USDC", "state": "live", "lotSz": "0.00001",
            "tickSz": "0.1", "minSz": "0.0001",
        }
        self.tk = {
            "instId": "BTC-USDC", "bidPx": "100", "askPx": "100.1",
            "ts": "1700000000000",
        }
        now = int(time.time() * 1000)
        self.candles_data = [
            [str(now + 60000), "1", "1", "1", "999", "1", "1", "1", "1"],  # future
            [str(now), "1", "1", "1", "105", "1", "1", "1", "0"],  # unconfirmed
            [str(now - 60000), "1", "1", "1", "101", "1", "1", "1", "1"],  # past
            [str(now - 120000), "1", "1", "1", "99", "1", "1", "1", "1"],  # past
        ]

    def instruments(self, inst_id):
        return self.inst

    def ticker(self, inst_id):
        return self.tk

    def candles(self, inst_id, bar="1m", limit=120):
        return self.candles_data


def test_market_precision_and_quote_shape():
    m = OKXMarket(("BTC-USDC",), FakeOKXMarketClient())
    q = m.snapshot()["BTC-USDC"]
    assert q.bid == D("100") and q.ask == D("100.1")
    assert q.timestamp == 1700000000.0
    assert q.base_increment == D("0.00001")
    assert q.quote_increment is None  # no quote-amount step on OKX spot
    assert q.price_increment == D("0.1")
    assert q.minimum_base == D("0.0001")
    assert q.minimum_quote == D("1")  # OKX's unpublished 1 USDT minimum order value (sCode 51020)


def test_completed_past_candles_only_and_no_double_record():
    m = OKXMarket(("BTC-USDC",), FakeOKXMarketClient())
    quotes = m.snapshot()
    assert m.history["BTC-USDC"] == [99.0, 101.0]  # oldest-first, confirmed only
    m.record(quotes)
    assert m.history["BTC-USDC"] == [99.0, 101.0, 100.05]
    m.snapshot()  # execution refresh must not add a neural observation
    assert len(m.history["BTC-USDC"]) == 3


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.inst.update(state="suspend"),
        lambda c: c.inst.update(instType="MARGIN"),
        lambda c: c.inst.update(quoteCcy="USDT"),
        lambda c: c.inst.update(baseCcy="ETH"),
        lambda c: c.inst.update(instId="ETH-USDC"),
        lambda c: c.tk.update(bidPx=""),
        lambda c: c.tk.update(ts=""),
        lambda c: c.tk.update(instId="ETH-USDC"),
    ],
)
def test_market_abnormal_data_fails_hard(mutate):
    c = FakeOKXMarketClient()
    mutate(c)
    with pytest.raises(RuntimeError):
        OKXMarket(("BTC-USDC",), c).snapshot()


def test_no_completed_candles_fails():
    c = FakeOKXMarketClient()
    c.candles_data = [[str(int(time.time() * 1000)), "1", "1", "1", "1", "1", "1", "1", "0"]]
    with pytest.raises(RuntimeError):
        OKXMarket(("BTC-USDC",), c).snapshot()


def test_invalid_historical_price_fails():
    c = FakeOKXMarketClient()
    now = int(time.time() * 1000)
    c.candles_data = [[str(now - 60000), "1", "1", "1", "0", "1", "1", "1", "1"]]
    with pytest.raises(RuntimeError):
        OKXMarket(("BTC-USDC",), c).snapshot()


# ---------------------------------------------------------------------------
# OKXBroker: demo credential isolation, live rejection, execution, reconcile
# ---------------------------------------------------------------------------

class FakeOKX(OrderQueryDoubles):
    """Models the OKX v5 endpoints OKXBroker calls; never opens a socket."""

    def __init__(self, demo=True):
        self.demo = demo
        self.api_key = "K" if demo else None
        self.secret = "S" if demo else None
        self.passphrase = "P" if demo else None
        self.acct = dict(ACCOUNT_CONFIG)
        # Gifted demo account: plenty of USDT plus unrelated BTC/ETH/OKB. The
        # bot must allocate only 100 USDT and leave the rest untouched.
        self.balances = {
            "USDT": D("1000"), "BTC": D("1"), "ETH": D("0.5"), "OKB": D("10"),
        }
        self.order_index = 0
        self.orders = {}  # clOrdId -> order
        self.orders_by_oid = {}  # ordId -> order
        self.untriggered_algos = []  # what the untriggered listing returns
        self.algo_pending_transient = ()
        self.archive_orders = []
        self.open_positions = []  # derivative/margin positions
        self.submissions = []
        self.place_response = None
        self.fee = "-0.05"
        self.fee_ccy = "USDT"
        self.rebate = "0"
        self.rebate_ccy = ""
        self.capture = None  # optional hook run inside place_order

    def account_config(self):
        return self.acct

    def balance(self, ccys=None):
        details = [_detail(c, self.balances.get(c, D("0"))) for c in self.balances]
        return [{"details": details}]

    def positions(self, inst_type=None):
        return list(self.open_positions)

    def account_position_risk(self, inst_type):
        return risk_snapshot(posData=list(self.open_positions))

    def place_order(self, payload):
        if self.capture:
            self.capture(payload)
        self.submissions.append(payload)
        if self.place_response is not None:
            return self.place_response
        self.order_index += 1
        oid = f"ord-{self.order_index}"
        order = {
            "ordId": oid,
            "clOrdId": payload["clOrdId"],
            "instId": payload["instId"],
            "side": payload["side"],
            "state": "filled",
            "accFillSz": payload["sz"],
            "avgPx": payload["px"],
            "fee": self.fee,
            "feeCcy": self.fee_ccy,
            "rebate": self.rebate,
            "rebateCcy": self.rebate_ccy,
        }
        self.orders[payload["clOrdId"]] = order
        self.orders_by_oid[oid] = order
        return {"code": "0", "data": [{"sCode": "0", "ordId": oid, "clOrdId": payload["clOrdId"]}]}

    def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        if ord_id:
            return self.orders_by_oid.get(ord_id)
        return self.orders.get(cl_ord_id)


@pytest.fixture
def okx_env(tmp_path):
    s = Settings(products=("BTC-USDT",))
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "okx-demo")
    guard = Guard(s, ledger, tmp_path / "STOP")
    fake = FakeOKX()
    broker = OKXBroker(s, ledger, fake)
    yield s, ledger, guard, fake, broker
    ledger.close()


def test_live_execution_is_rejected(monkeypatch, tmp_path):
    s = Settings()
    ledger = Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    monkeypatch.setenv("OKX_LIVE", "I_ACCEPT_REAL_TRADES")
    with pytest.raises(RuntimeError, match="live"):
        OKXBroker.from_env(s, ledger)
    ledger.close()


def test_demo_requires_credentials(monkeypatch, tmp_path):
    s = Settings()
    ledger = Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE", "OKX_LIVE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="OKX_API_KEY"):
        OKXBroker.from_env(s, ledger)
    ledger.close()


def test_from_env_builds_demo_client(monkeypatch, tmp_path):
    s = Settings()
    ledger = Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    monkeypatch.setenv("OKX_API_KEY", "K")
    monkeypatch.setenv("OKX_API_SECRET", "S")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "P")
    monkeypatch.delenv("OKX_LIVE", raising=False)
    broker = OKXBroker.from_env(s, ledger)
    assert broker.client.demo is True
    assert broker.client.api_key == "K"
    assert broker.client.secret == "S"
    assert broker.client.passphrase == "P"
    ledger.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.acct.update(acctLv="3"),
        lambda f: f.acct.update(acctLv="4"),
        lambda f: f.acct.update(acctLv=""),
        lambda f: f.acct.update(perm="read"),
        lambda f: f.acct.update(perm="read,trade,withdraw"),
        lambda f: f.acct.update(uid=""),
        lambda f: f.acct.update(mainUid=""),
        # A sub-account must not be adopted silently.
        lambda f: f.acct.update(mainUid="uid-2"),
        lambda f: f.acct.update(type="1"),
        # Borrowing must be off and present, not merely assumed absent.
        lambda f: f.acct.update(enableSpotBorrow=True),
        lambda f: f.acct.update(autoLoan=True),
        lambda f: f.acct.update(spotBorrowAutoRepay=True),
        lambda f: f.acct.pop("enableSpotBorrow"),
        lambda f: f.acct.update(autoLoan="false"),  # a string is not a bool
    ],
)
def test_preflight_account_mode_and_permissions(okx_env, mutate):
    _, _, _, fake, broker = okx_env
    mutate(fake)
    with pytest.raises(RuntimeError):
        broker.preflight()


def test_preflight_initializes_budget(okx_env):
    _, l, _, _, broker = okx_env
    result = broker.preflight()
    assert result["mode"] == "okx-demo"
    # No account identifier is exposed: it is bound in the ledger, not printed.
    assert result["account_bound"] is True
    assert "account" not in result
    assert result["spot_cash_mode"] is True
    assert result["account_level"] == "2"
    assert result["quote_ccy"] == "USDT"
    assert result["algo_coverage"] == "verified"
    assert "orders-algo-pending" in result["algo_coverage_source"]
    assert result["algo_coverage_types_verified"]
    assert l.get("demo_initialized") is True
    assert l.cash == D("100")
    assert l.get("budget") == "100"
    # Only the 100 USDT budget is allocable; gifted BTC/ETH/OKB stay in baseline.
    assert l.get("baseline") == {
        "USDT": "1000", "BTC": "1", "ETH": "0.5", "OKB": "10",
    }


def test_preflight_allows_gifted_assets_untouched(okx_env):
    _, l, _, _, broker = okx_env
    broker.preflight()
    assert l.positions == {}  # gifted BTC/ETH/OKB are not bot inventory
    assert l.cash == D("100")  # only the 100 USDT budget is spendable


def test_preflight_rejects_insufficient_quote(okx_env):
    _, _, _, fake, broker = okx_env
    fake.balances["USDT"] = D("50")  # below the 100 USDT budget
    with pytest.raises(RuntimeError, match="Insufficient"):
        broker.preflight()


def test_verify_balances_detects_external_quote_change(okx_env):
    _, _, _, fake, broker = okx_env
    broker.preflight()
    fake.balances["USDT"] += D("5")
    with pytest.raises(RuntimeError, match="balance"):
        broker.verify_balances()


def test_verify_balances_detects_unallocated_asset_change(okx_env):
    _, _, _, fake, broker = okx_env
    broker.preflight()
    fake.balances["BTC"] = D("1.5")  # gifted BTC moved without bot activity
    with pytest.raises(RuntimeError, match="balance"):
        broker.verify_balances()


def test_verify_balances_detects_open_order(okx_env):
    _, _, _, fake, broker = okx_env
    broker.preflight()
    fake.open_orders = [{"ordId": "x"}]
    with pytest.raises(RuntimeError, match="open order"):
        broker.verify_balances()


def test_verify_balances_detects_algo_order(okx_env):
    _, _, _, fake, broker = okx_env
    broker.preflight()
    fake.untriggered_algos = [{"algoId": "a", "ordType": "trigger"}]
    with pytest.raises(RuntimeError, match="algo"):
        broker.verify_balances()


def test_verify_balances_detects_positions(okx_env):
    _, _, _, fake, broker = okx_env
    broker.preflight()
    fake.open_positions = [{"posId": "p"}]
    with pytest.raises(RuntimeError, match="positions"):
        broker.verify_balances()


@pytest.mark.parametrize(
    "field",
    ["liab", "crossLiab", "isoLiab", "interest", "frozenBal", "ordFrozen"],
)
def test_preflight_rejects_nonzero_risk_field(okx_env, field):
    _, _, _, fake, broker = okx_env

    def balance_with_risk():
        details = [_detail(c, fake.balances.get(c, D("0"))) for c in fake.balances]
        for d in details:
            if d["ccy"] == "USDT":
                d[field] = "1"
        return [{"details": details}]

    fake.balance = balance_with_risk
    with pytest.raises(RuntimeError, match=field):
        broker.preflight()


def test_preflight_allows_empty_risk_fields(okx_env):
    _, l, _, fake, broker = okx_env
    # OKX encodes "no liability/borrow" as "" (empty string); a clean spot
    # account must be accepted, not mistaken for missing data.
    result = broker.preflight()
    assert result["mode"] == "okx-demo"
    assert l.get("demo_initialized") is True


def test_preflight_rejects_unparseable_risk_field(okx_env):
    _, _, _, fake, broker = okx_env

    def balance_garbage():
        details = [_detail(c, fake.balances.get(c, D("0"))) for c in fake.balances]
        for d in details:
            if d["ccy"] == "USDT":
                d["liab"] = "n/a"
        return [{"details": details}]

    fake.balance = balance_garbage
    with pytest.raises(RuntimeError, match="Unparseable risk field"):
        broker.preflight()


def test_buy_settles_and_persists_unknown_before_send(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    seen = {}
    fake.capture = lambda payload: seen.update(status=[r["status"] for r in l.pending()])
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "SETTLED"
    assert seen["status"] == ["UNKNOWN"]  # durable UNKNOWN precedes the request
    assert fake.submissions[0]["tdMode"] == "cash"
    assert fake.submissions[0]["ordType"] == "fok"
    # Band safety: an undisclosed dynamic price limit must amend, not reject.
    assert fake.submissions[0]["pxAmendType"] == "1"
    assert "-" not in plan["client_order_id"] and len(plan["client_order_id"]) == 32
    assert l.cash < D("100")
    assert l.positions["BTC-USDT"] == D(fake.submissions[0]["sz"])


def test_sell_settles(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    # Simulate a prior bot buy: 0.2 BTC at 100 USDT, funded from the bot's budget.
    l.put("positions", {"BTC-USDT": "0.2"})
    l.put("cash", "80")
    fake.balances["USDT"] = D("980")
    fake.balances["BTC"] = D("1.2")  # gifted 1 + bot-bought 0.2
    plan = l.reserve(g.plan("BTC-USDT", "SELL", {"BTC-USDT": quote()}), time.time())
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "SETTLED"
    assert fake.submissions[0]["side"] == "sell"
    assert l.cash > D("80")
    assert l.positions["BTC-USDT"] < D("0.2")


def test_base_currency_fee(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.fee = "-0.001"
    fake.fee_ccy = "BTC"
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    broker.execute(plan, g.before_submit)
    size = D(fake.submissions[0]["sz"])
    assert l.positions["BTC-USDT"] == size - D("0.001")  # base fee reduces base received


def test_rebate_is_credit_not_charge(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.fee = "0"
    fake.rebate = "0.05"
    fake.rebate_ccy = "USDT"
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    broker.execute(plan, g.before_submit)
    size = D(fake.submissions[0]["sz"])
    value = size * D(fake.submissions[0]["px"])
    assert l.cash == D("100") - value + D("0.05")


def test_split_fee_and_rebate_ccy_unresolved(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.fee = "-0.001"
    fake.fee_ccy = "BTC"
    fake.rebate = "0.05"
    fake.rebate_ccy = "USDT"
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)


def test_unsupported_fee_ccy_unresolved(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.fee = "-0.001"
    fake.fee_ccy = "ETH"  # neither base nor quote
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)


def test_fee_overrun_records_fill_then_halts(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.fee = "-1"  # exceeds the previewed ceiling
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    broker.execute(plan, g.before_submit)
    assert "fee exceeded" in l.get("halted")
    assert not l.pending()  # fill is recorded despite the halt


def test_buy_reconciles_against_mirrored_exchange(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "SETTLED"
    size = D(fake.submissions[0]["sz"])
    px = D(fake.submissions[0]["px"])
    # Mirror the fill on the exchange: quote spent (incl. fee), base received.
    fake.balances["USDT"] = D("1000") - size * px - D("0.05")
    fake.balances["BTC"] = D("1") + size
    broker.verify_balances()  # reconciliation must pass against the mirrored state


def test_top_level_request_failure_is_unresolved(okx_env):
    # A non-zero top-level code with NO per-order result is an ambiguous
    # request-level outcome (OKX does not confirm whether an order was placed),
    # so it stays UNKNOWN, never REJECTED.
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.place_response = {"code": "51000", "msg": "invalid param", "data": []}
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    with pytest.raises(UnresolvedOrder) as raised:
        broker.execute(plan, g.before_submit)
    assert l.pending()[0]["status"] == "UNKNOWN"
    # The exchange's own top-level code is recorded so an ambiguous submission is
    # diagnosable afterwards; the response body must not travel with it.
    assert raised.value.okx_code == "51000"


@pytest.mark.parametrize(
    "resp",
    [
        # The envelope says the operation failed / partially succeeded, but the
        # item still carries the authoritative per-order result. OKX's General
        # Information rule: when data has sCode, sCode -- not the top-level code
        # -- represents the result for that order. This is exactly the SELL
        # failure shape: an answered rejection must never become an unresolved
        # halt that needs a human adjudication.
        {"code": "1", "msg": "Operation failed", "data": [
            {"sCode": "51008", "sMsg": "Order cost or size is greater than the maximum"}
        ]},
        {"code": "2", "msg": "Bulk operation partially succeeded.", "data": [
            {"sCode": "51008", "sMsg": ""}
        ]},
    ],
)
def test_envelope_failure_with_a_per_order_result_is_definite(okx_env, resp):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    # A sellable position, so the guard proposes the SELL.
    l.put("positions", {"BTC-USDT": "0.2"})
    l.put("cash", "80")
    fake.balances["USDT"] = D("980")
    fake.balances["BTC"] = D("1.2")
    fake.place_response = resp
    plan = l.reserve(g.plan("BTC-USDT", "SELL", {"BTC-USDT": quote()}), time.time())
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "REJECTED"
    assert not l.pending()
    assert len(fake.submissions) == 1
    # The recorded codes name the exchange's reason without echoing the body.
    assert result["okx_code"] == resp["code"]
    assert result["order_scode"] == "51008"


def test_timeout_envelope_with_a_resultless_item_is_unresolved(okx_env):
    # A timeout envelope carries no per-order answer even when an item exists:
    # nothing about the order's outcome is known, so it stays UNKNOWN.
    s, l, g, fake, broker = okx_env
    broker.preflight()
    l.put("positions", {"BTC-USDT": "0.2"})
    l.put("cash", "80")
    fake.balances["USDT"] = D("980")
    fake.balances["BTC"] = D("1.2")
    fake.place_response = {"code": "50004", "msg": "timeout", "data": [{"clOrdId": "x"}]}
    plan = l.reserve(g.plan("BTC-USDT", "SELL", {"BTC-USDT": quote()}), time.time())
    with pytest.raises(UnresolvedOrder) as raised:
        broker.execute(plan, g.before_submit)
    assert l.pending()[0]["status"] == "UNKNOWN"
    assert raised.value.okx_code == "50004"


def test_success_envelope_without_a_result_code_is_unresolved(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.place_response = {"code": "0", "data": [{"ordId": "x"}]}
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    with pytest.raises(UnresolvedOrder) as raised:
        broker.execute(plan, g.before_submit)
    assert l.pending()[0]["status"] == "UNKNOWN"
    assert raised.value.okx_code == "0"


def test_http_403_on_place_order_is_unresolved_not_resent(okx_env):
    # A transport-level 403 (e.g. CDN rejection) is an ambiguous outcome too:
    # the order stays UNKNOWN and is submitted exactly once, never resubmitted.
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())

    calls = []

    def forbidden(payload):
        calls.append(payload)
        raise OKXTransportError("OKX HTTP error", status=403)

    fake.place_order = forbidden
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert len(calls) == 1  # no retry / resend
    assert l.pending()[0]["status"] == "UNKNOWN"


def test_order_level_error_is_rejected(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.place_response = {"code": "0", "data": [{"sCode": "51008", "sMsg": "no funds"}]}
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "REJECTED"
    assert not l.pending()


def test_generic_operation_failure_is_unresolved(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.place_response = {"code": "1", "msg": "operation failed", "data": []}
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert l.pending()  # stays unresolved; never resubmitted


def test_final_guard_veto_before_send(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    g.stop_file.touch()
    with pytest.raises(Veto):
        broker.execute(plan, g.before_submit)
    assert fake.submissions == []
    assert not l.pending()


def test_post_accept_timeout_is_unresolved(okx_env, monkeypatch):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    t = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(time, "sleep", lambda s: t.__setitem__(0, t[0] + s))
    state = {}

    def place(payload):
        state["cid"] = payload["clOrdId"]
        fake.submissions.append(payload)
        return {"code": "0", "data": [{"sCode": "0", "ordId": "ord-x", "clOrdId": payload["clOrdId"]}]}

    fake.place_order = place
    fake.get_order = lambda *a, **k: {
        "ordId": "ord-x", "clOrdId": state["cid"], "instId": "BTC-USDT",
        "side": "buy", "state": "live",
    }
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert l.pending()[0]["status"] == "ACCEPTED"


def test_identity_mismatch_is_unresolved(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())

    def poisoned(payload):
        fake.submissions.append(payload)
        fake.orders_by_oid["ord-1"] = {
            "ordId": "ord-1", "clOrdId": "WRONG", "instId": "BTC-USDT",
            "side": "buy", "state": "filled", "accFillSz": payload["sz"],
            "avgPx": payload["px"], "fee": "-0.05", "feeCcy": "USDT",
            "rebate": "0", "rebateCcy": "",
        }
        return {"code": "0", "data": [{"sCode": "0", "ordId": "ord-1", "clOrdId": payload["clOrdId"]}]}

    fake.place_order = poisoned
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert l.pending()


def test_not_found_is_not_inferred_as_no_fill(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()

    def place(payload):
        fake.submissions.append(payload)
        return {"code": "0", "data": [{"sCode": "0", "ordId": "ord-9", "clOrdId": payload["clOrdId"]}]}

    fake.place_order = place
    fake.get_order = lambda *a, **k: None
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert l.pending()


def test_reconcile_restart_no_duplicate_or_resubmit(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    # Crash after acceptance, before settle: mark UNKNOWN then ACCEPTED.
    l.mark(plan["client_order_id"], "UNKNOWN")
    oid = "ord-100"
    l.mark(plan["client_order_id"], "ACCEPTED", oid)
    fake.orders_by_oid[oid] = {
        "ordId": oid, "clOrdId": plan["client_order_id"], "instId": "BTC-USDT",
        "side": "buy", "state": "filled", "accFillSz": plan["base_size"],
        "avgPx": plan["limit_price"], "fee": "-0.05", "feeCcy": "USDT",
        "rebate": "0", "rebateCcy": "",
    }
    fresh = Ledger(l.path, s, "okx-demo")
    broker2 = OKXBroker(s, fresh, fake)
    broker2.reconcile()
    assert not fresh.pending()
    assert fake.submissions == []  # reconcile must not resubmit
    cash_after = fresh.cash
    broker2.reconcile()
    assert fresh.cash == cash_after  # idempotent
    fresh.close()


def test_restart_does_not_reallocate_budget(okx_env):
    s, l, _, fake, broker = okx_env
    broker.preflight()
    baseline = l.get("baseline")
    budget = l.get("budget")
    cash = l.cash
    # Reopen the ledger (as on restart) and preflight again: no re-allocation.
    fresh = Ledger(l.path, s, "okx-demo")
    broker2 = OKXBroker(s, fresh, fake)
    broker2.preflight()
    assert fresh.get("baseline") == baseline
    assert fresh.get("budget") == budget
    assert fresh.cash == cash
    assert fresh.get("demo_initialized") is True
    fresh.close()


def test_reconcile_prepared_is_rejected(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    broker.reconcile()  # PREPARED cannot have been sent
    assert not l.pending()
    assert fake.submissions == []


def test_reconcile_uncertain_not_found_is_unresolved(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    l.mark(plan["client_order_id"], "UNKNOWN")
    fake.orders = {}  # no order known by clOrdId
    with pytest.raises(UnresolvedOrder):
        broker.reconcile()
    assert l.pending()


def test_cannot_sell_gifted_btc_without_inventory(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    # The account holds 1 gifted BTC, but the bot has no BTC inventory of its
    # own, so a SELL must be vetoed rather than spending gifted assets.
    with pytest.raises(Veto, match="position|funds"):
        g.plan("BTC-USDT", "SELL", {"BTC-USDT": quote()})
    assert l.positions == {}


def test_unallocated_asset_price_not_in_equity(okx_env):
    s, l, _, fake, broker = okx_env
    broker.preflight()
    q_low = {"BTC-USDT": quote()}
    q_high = {
        "BTC-USDT": Quote(
            "BTC-USDT", D("200"), D("200.1"), time.time(),
            D(".00000001"), D(".01"), D(".01"), D("1"), D(".00000001"),
        )
    }
    # Gifted BTC sits in baseline, not bot positions, so a BTC price move never
    # changes the bot's equity (and therefore never its reinforcement signal).
    assert l.equity(q_low) == l.equity(q_high) == D("100")


def test_ledger_identity_mismatch_rejected(tmp_path):
    s = Settings()
    l1 = Ledger(tmp_path / "x.sqlite", s, "paper")
    l1.close()
    with pytest.raises(RuntimeError, match="mismatch"):
        Ledger(tmp_path / "x.sqlite", s, "okx-demo")


def test_account_binding_enforced(okx_env):
    _, l, _, fake, broker = okx_env
    broker.preflight()
    assert l.get("identity")["account"] == "uid-1"
    with pytest.raises(RuntimeError, match="mismatch"):
        l.bind_account("uid-2")


def test_reserve_uses_legal_clordid(okx_env):
    _, l, g, _, _ = okx_env
    plan = l.reserve(g.plan("BTC-USDT", "BUY", {"BTC-USDT": quote()}), time.time())
    cid = plan["client_order_id"]
    assert "-" not in cid and cid.isalnum() and len(cid) <= 32
