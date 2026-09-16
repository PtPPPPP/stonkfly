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


def quote():
    return Quote(
        "BTC-USDC", D("100"), D("100.1"), time.time(),
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


def test_http_error_preserves_status_and_cause(monkeypatch):
    # _http converts a urllib HTTPError (e.g. a 502 returned by a proxy) into an
    # OKXTransportError that carries both the status code and the underlying
    # HTTPError, so a diagnostic can tell a 502 apart from a connection reset.
    http_error = urllib.error.HTTPError(
        "https://www.okx.com/api/v5/public/time", 502, "Bad Gateway", {}, None
    )

    def fake_urlopen(req, timeout=None):
        raise http_error

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = OKXClient()
    with pytest.raises(OKXTransportError) as ei:
        client._http("GET", "https://www.okx.com/api/v5/public/time", {}, None)
    assert ei.value.status == 502
    assert ei.value.__cause__ is http_error


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
    assert q.minimum_quote is None  # no quote minimum on OKX spot


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

class FakeOKX:
    """Models the OKX v5 endpoints OKXBroker calls; never opens a socket."""

    def __init__(self, demo=True):
        self.demo = demo
        self.api_key = "K" if demo else None
        self.secret = "S" if demo else None
        self.passphrase = "P" if demo else None
        self.acct = {"acctLv": "1", "perm": "read,trade", "uid": "uid-1"}
        self.balances = {"USDC": D("100"), "BTC": D("0")}
        self.order_index = 0
        self.orders = {}  # clOrdId -> order
        self.orders_by_oid = {}  # ordId -> order
        self.pending = []
        self.submissions = []
        self.place_response = None
        self.fee = "-0.05"
        self.fee_ccy = "USDC"
        self.rebate = "0"
        self.rebate_ccy = ""
        self.capture = None  # optional hook run inside place_order

    def account_config(self):
        return self.acct

    def balance(self, ccys):
        details = [
            {
                "ccy": c,
                "availBal": str(self.balances.get(c, D("0"))),
                "frozenBal": "0",
                "ordFrozen": "0",
            }
            for c in ccys
        ]
        return [{"details": details}]

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

    def orders_pending(self, inst_id):
        return self.pending


@pytest.fixture
def okx_env(tmp_path):
    s = Settings()
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
        lambda f: f.acct.update(acctLv="2"),
        lambda f: f.acct.update(perm="read"),
        lambda f: f.acct.update(perm="read,trade,withdraw"),
        lambda f: f.acct.update(uid=""),
    ],
)
def test_preflight_account_mode_and_permissions(okx_env, mutate):
    _, _, _, fake, broker = okx_env
    mutate(fake)
    with pytest.raises(RuntimeError):
        broker.preflight()


def test_preflight_initializes_demo_with_usdc_only(okx_env):
    _, l, _, _, broker = okx_env
    result = broker.preflight()
    assert result["mode"] == "okx-demo"
    assert result["account"] == "uid-1"
    assert result["spot_cash_mode"] is True
    assert l.get("demo_initialized") is True
    assert l.cash == D("100")


def test_preflight_rejects_non_usdc_seed(okx_env):
    _, _, _, fake, broker = okx_env
    fake.balances["BTC"] = D("1")
    with pytest.raises(RuntimeError, match="USDC"):
        broker.preflight()


def test_preflight_rejects_overfunded(okx_env):
    _, _, _, fake, broker = okx_env
    fake.balances["USDC"] = D("101")
    with pytest.raises(RuntimeError):
        broker.preflight()


def test_verify_balances_detects_external_change(okx_env):
    _, _, _, fake, broker = okx_env
    broker.preflight()
    fake.balances["USDC"] += D("5")
    with pytest.raises(RuntimeError, match="balance"):
        broker.verify_balances()


def test_verify_balances_detects_open_order(okx_env):
    _, _, _, fake, broker = okx_env
    broker.preflight()
    fake.pending = [{"ordId": "x"}]
    with pytest.raises(RuntimeError, match="open order"):
        broker.verify_balances()


def test_buy_settles_and_persists_unknown_before_send(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    seen = {}
    fake.capture = lambda payload: seen.update(status=[r["status"] for r in l.pending()])
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "SETTLED"
    assert seen["status"] == ["UNKNOWN"]  # durable UNKNOWN precedes the request
    assert fake.submissions[0]["tdMode"] == "cash"
    assert fake.submissions[0]["ordType"] == "fok"
    assert "-" not in plan["client_order_id"] and len(plan["client_order_id"]) == 32
    assert l.cash < D("100")
    assert l.positions["BTC-USDC"] == D(fake.submissions[0]["sz"])


def test_sell_settles(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    l.put("positions", {"BTC-USDC": "0.2"})
    fake.balances["BTC"] = D("0.2")  # external account mirrors the ledger position
    plan = l.reserve(g.plan("BTC-USDC", "SELL", {"BTC-USDC": quote()}), time.time())
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "SETTLED"
    assert fake.submissions[0]["side"] == "sell"
    assert l.cash > D("100")
    assert l.positions["BTC-USDC"] < D("0.2")


def test_base_currency_fee(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.fee = "-0.001"
    fake.fee_ccy = "BTC"
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    broker.execute(plan, g.before_submit)
    size = D(fake.submissions[0]["sz"])
    assert l.positions["BTC-USDC"] == size - D("0.001")  # base fee reduces base received


def test_rebate_is_credit_not_charge(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.fee = "0"
    fake.rebate = "0.05"
    fake.rebate_ccy = "USDC"
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
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
    fake.rebate_ccy = "USDC"
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)


def test_unsupported_fee_ccy_unresolved(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.fee = "-0.001"
    fake.fee_ccy = "ETH"  # neither base nor quote
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)


def test_fee_overrun_records_fill_then_halts(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.fee = "-1"  # exceeds the previewed ceiling
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    broker.execute(plan, g.before_submit)
    assert "fee exceeded" in l.get("halted")
    assert not l.pending()  # fill is recorded despite the halt


def test_top_level_request_failure_is_unresolved(okx_env):
    # A non-zero top-level code is an ambiguous request-level outcome (OKX
    # does not confirm whether an order was placed), so it stays UNKNOWN,
    # never REJECTED. Only code=="0" with a non-zero sCode is a definite
    # rejection.
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.place_response = {"code": "51000", "msg": "invalid param", "data": []}
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert l.pending()[0]["status"] == "UNKNOWN"


def test_http_403_on_place_order_is_unresolved_not_resent(okx_env):
    # A transport-level 403 (e.g. CDN rejection) is an ambiguous outcome too:
    # the order stays UNKNOWN and is submitted exactly once, never resubmitted.
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())

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
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    result = broker.execute(plan, g.before_submit)
    assert result["status"] == "REJECTED"
    assert not l.pending()


def test_generic_operation_failure_is_unresolved(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    fake.place_response = {"code": "1", "msg": "operation failed", "data": []}
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert l.pending()  # stays unresolved; never resubmitted


def test_final_guard_veto_before_send(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
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
        "ordId": "ord-x", "clOrdId": state["cid"], "instId": "BTC-USDC",
        "side": "buy", "state": "live",
    }
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert l.pending()[0]["status"] == "ACCEPTED"


def test_identity_mismatch_is_unresolved(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())

    def poisoned(payload):
        fake.submissions.append(payload)
        fake.orders_by_oid["ord-1"] = {
            "ordId": "ord-1", "clOrdId": "WRONG", "instId": "BTC-USDC",
            "side": "buy", "state": "filled", "accFillSz": payload["sz"],
            "avgPx": payload["px"], "fee": "-0.05", "feeCcy": "USDC",
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
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    with pytest.raises(UnresolvedOrder):
        broker.execute(plan, g.before_submit)
    assert l.pending()


def test_reconcile_restart_no_duplicate_or_resubmit(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    # Crash after acceptance, before settle: mark UNKNOWN then ACCEPTED.
    l.mark(plan["client_order_id"], "UNKNOWN")
    oid = "ord-100"
    l.mark(plan["client_order_id"], "ACCEPTED", oid)
    fake.orders_by_oid[oid] = {
        "ordId": oid, "clOrdId": plan["client_order_id"], "instId": "BTC-USDC",
        "side": "buy", "state": "filled", "accFillSz": plan["base_size"],
        "avgPx": plan["limit_price"], "fee": "-0.05", "feeCcy": "USDC",
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


def test_reconcile_prepared_is_rejected(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    broker.reconcile()  # PREPARED cannot have been sent
    assert not l.pending()
    assert fake.submissions == []


def test_reconcile_uncertain_not_found_is_unresolved(okx_env):
    s, l, g, fake, broker = okx_env
    broker.preflight()
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    l.mark(plan["client_order_id"], "UNKNOWN")
    fake.orders = {}  # no order known by clOrdId
    with pytest.raises(UnresolvedOrder):
        broker.reconcile()
    assert l.pending()


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
    plan = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    cid = plan["client_order_id"]
    assert "-" not in cid and cid.isalnum() and len(cid) <= 32
