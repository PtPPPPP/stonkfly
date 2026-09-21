"""Account risk model, algo-order coverage, and output sanitization.

Counter-example tests for the OKX adapter's balance/risk validation and for the
algo-order listing gap OKX's demo environment exhibits. Every exchange call is
an in-memory double; no network or credentials are used.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import time

import pytest

from stonkfly.config import D, Settings
from stonkfly.ledger import Ledger
from stonkfly.market import Quote
from stonkfly.okx_broker import OKXAlgoCoverageUnverified, OKXBroker, OKXRiskError
from stonkfly.okx_client import OKXBusinessError, OKXTransportError
from stonkfly.risk import Guard

from okx_doubles import ACCOUNT_CONFIG, OrderQueryDoubles, detail, risk_snapshot


def quote():
    return Quote(
        "BTC-USDT", D("100"), D("100.1"), time.time(),
        D(".00000001"), D(".01"), D(".01"), D("1"), D(".00000001"),
    )


class FakeOKXAccount(OrderQueryDoubles):
    """Minimal demo account double for the account/risk endpoints."""

    def __init__(self):
        self.demo = True
        self.acct = dict(ACCOUNT_CONFIG)
        self.balances = {"USDT": D("100"), "BTC": D("0")}
        self.submissions = []
        self.open_orders = []
        self.untriggered_algos = []
        self.algo_pending_transient = ()
        self.archive_orders = []
        self.open_positions = []
        self.extra_detail = {}

    def account_config(self):
        return self.acct

    def balance(self, ccys=None):
        return [
            {
                "details": [
                    detail(c, v, **self.extra_detail)
                    for c, v in self.balances.items()
                ]
            }
        ]

    def positions(self, inst_type=None):
        return list(self.open_positions)

    def account_position_risk(self, inst_type):
        return risk_snapshot(posData=list(self.open_positions))

    def place_order(self, payload):
        self.submissions.append(payload)
        return {"code": "0", "data": [{"sCode": "0", "ordId": "ord-1"}]}


@pytest.fixture
def env(tmp_path):
    s = Settings(products=("BTC-USDT",))
    l = Ledger(tmp_path / "ledger.sqlite", s, "okx-demo")
    g = Guard(s, l, tmp_path / "STOP")
    fake = FakeOKXAccount()
    broker = OKXBroker(s, l, fake)
    yield s, l, g, fake, broker, tmp_path
    l.close()


ALGO_TYPES = (
    "conditional", "oco", "trigger", "move_order_stop", "iceberg", "twap",
    "smart_iceberg",
)


def _only_these_answer(answered=(), code="51054"):
    def query(ord_type, retries=3):
        if ord_type in answered:
            return []
        raise OKXBusinessError(f"orders algo pending: OKX code={code}", code=code)
    return query


# ---------------------------------------------------------------------------
# Balance-detail field model.
# ---------------------------------------------------------------------------

def test_missing_risk_field_is_a_hard_stop(env):
    # A field the adapter cannot see is a field it cannot verify. Absence must
    # never be read as "no risk" -- only a present "" is "not applicable".
    s, l, g, fake, broker, _ = env
    for field in ("liab", "interest", "frozenBal", "autoLendAmt"):
        fake.balances["USDT"] = D("100")

        def balance(ccys=None, _f=field):
            details = [
                {k: v for k, v in detail(c, amt).items() if k != _f}
                for c, amt in fake.balances.items()
            ]
            return [{"details": details}]

        fake.balance = balance
        with pytest.raises(RuntimeError, match="absent"):
            broker.preflight()


def test_negative_required_balance_is_a_hard_stop(env):
    s, l, g, fake, broker, _ = env
    fake.extra_detail = {"cashBal": "-1"}
    with pytest.raises(RuntimeError, match="cashBal"):
        broker.preflight()


def test_empty_required_balance_is_not_read_as_zero(env):
    # "" is not a documented encoding for a balance field, so it must fail
    # loudly instead of being defaulted to zero.
    s, l, g, fake, broker, _ = env
    fake.extra_detail = {"availBal": ""}
    with pytest.raises(RuntimeError, match="availBal"):
        broker.preflight()


def test_borrow_capacity_and_margin_ratio_are_not_required_zero(env):
    # maxLoan is a maximum *borrowable* amount and mgnRatio is a *ratio*: a
    # positive value on a cash-only account with no positions is not exposure,
    # so neither may be forced to zero the way a liability is.
    s, l, g, fake, broker, _ = env
    fake.extra_detail = {"maxLoan": "500", "mgnRatio": "1.5", "disEq": "12"}
    assert broker.preflight()["spot_cash_mode"] is True


@pytest.mark.parametrize("field,value", [("maxLoan", "-1"), ("mgnRatio", "n/a")])
def test_invalid_quantitative_field_still_fails(env, field, value):
    s, l, g, fake, broker, _ = env
    fake.extra_detail = {field: value}
    with pytest.raises(RuntimeError, match=field):
        broker.preflight()


@pytest.mark.parametrize(
    "field,value", [("autoLendStatus", "active"), ("autoStakingStatus", "active")]
)
def test_active_lending_or_staking_state_is_a_hard_stop(env, field, value):
    s, l, g, fake, broker, _ = env
    fake.extra_detail = {field: value}
    with pytest.raises(RuntimeError, match=field):
        broker.preflight()


# ---------------------------------------------------------------------------
# Total vs available funds.
# ---------------------------------------------------------------------------

def test_total_balance_sufficient_but_available_insufficient(env):
    # cashBal is more than enough while availBal is zero: the money is
    # displayed but not spendable, so no budget may be carved out of it.
    s, l, g, fake, broker, _ = env
    fake.balances["USDT"] = D("1000")
    fake.extra_detail = {"availBal": "0"}
    with pytest.raises(RuntimeError, match="Insufficient available"):
        broker.preflight()
    assert l.get("demo_initialized") is None
    assert l.get("baseline") is None


def test_available_balance_must_keep_covering_the_budget(env):
    s, l, g, fake, broker, _ = env
    broker.preflight()
    fake.extra_detail = {"availBal": "0"}
    with pytest.raises(RuntimeError, match="covers"):
        broker.verify_balances()


def test_sell_requires_actually_available_base(env):
    # The bot owns base the exchange reports as cash but not as spendable: the
    # sell is refused at the send boundary, not discovered after a fill.
    s, l, g, fake, broker, _ = env
    broker.preflight()
    l.put("positions", {"BTC-USDT": "0.1"})
    l.put("cash", "90")
    fake.balances = {"USDT": D("90"), "BTC": D("0.1")}
    fake.balance = lambda ccys=None: [
        {
            "details": [
                detail("USDT", D("90"), availBal="90"),
                detail("BTC", D("0.1"), availBal="0"),
            ]
        }
    ]
    l.put("last_attempt", 0)
    plan = l.reserve(g.plan("BTC-USDT", "SELL", {"BTC-USDT": quote()}), time.time())
    with pytest.raises(RuntimeError, match="does not cover"):
        broker.execute(plan, g.before_submit)
    assert fake.submissions == []
    assert not l.pending()


# ---------------------------------------------------------------------------
# Re-verification during the run.
# ---------------------------------------------------------------------------

def test_account_mode_is_reverified_every_tick(env):
    s, l, g, fake, broker, _ = env
    broker.preflight()
    fake.acct.update(acctLv="3")
    with pytest.raises(RuntimeError, match="mode"):
        broker.verify_balances()


def test_account_identity_is_reverified_every_tick(env):
    s, l, g, fake, broker, _ = env
    broker.preflight()
    fake.acct.update(uid="uid-2", mainUid="uid-2")
    with pytest.raises(RuntimeError, match="mismatch"):
        broker.verify_balances()


def test_positions_risk_snapshot_must_be_present(env):
    # A risk snapshot without a position list is unverifiable, not "no risk".
    s, l, g, fake, broker, _ = env
    fake.account_position_risk = lambda inst_type: {"ts": "1", "adjEq": ""}
    with pytest.raises(RuntimeError, match="position list"):
        broker.preflight()


def test_adjusted_equity_means_not_cash_only(env):
    s, l, g, fake, broker, _ = env
    fake.account_position_risk = lambda inst_type: risk_snapshot(adjEq="42")
    with pytest.raises(RuntimeError, match="cash-only"):
        broker.preflight()


# ---------------------------------------------------------------------------
# Algo/strategy order coverage.
# ---------------------------------------------------------------------------

def _listing(fake, transient=(), rows=(), code="51054"):
    """Record which ordTypes the untriggered listing is asked for.

    ``transient`` models the ordTypes OKX answers 51054 for, which is the case a
    substitute view must never be allowed to paper over.
    """
    asked = []

    def query(ord_type, retries=3):
        wanted = {part.strip() for part in ord_type.split(",") if part.strip()}
        asked.extend(sorted(wanted))
        if wanted & set(transient):
            raise OKXBusinessError(f"orders algo pending: OKX code={code}", code=code)
        return [o for o in rows if o.get("ordType") in wanted]

    fake.orders_algo_pending = query
    return asked


def test_coverage_comes_from_the_documented_untriggered_listing(env):
    # "Retrieve a list of untriggered Algo orders under the current account" is
    # the only documented view of the not-yet-triggered set, so every applicable
    # ordType must be asked for there.
    s, l, g, fake, broker, _ = env
    asked = _listing(fake)
    result = broker.preflight()
    assert set(asked) == set(ALGO_TYPES) | {"chase"}
    assert result["algo_coverage"] == "verified"
    assert "orders-algo-pending" in result["algo_coverage_source"]
    assert set(result["algo_coverage_types_verified"]) == set(asked)


@pytest.mark.parametrize("code", ["51054", "51010", "51000", "50013", "99999"])
def test_a_type_that_cannot_be_listed_stops_the_run(env, code):
    # 51054 is documented as "Request timed out", and no other business code is a
    # licence to skip a type. There is no substitute view and no acknowledgement.
    s, l, g, fake, broker, _ = env
    _listing(fake, transient=("trigger",), code=code)
    with pytest.raises(OKXAlgoCoverageUnverified) as ei:
        broker.preflight()
    assert [t for t, _c in ei.value.unverified] == ["trigger"]
    assert l.get("demo_initialized") is None
    assert l.get("baseline") is None


def test_unverified_types_are_named_in_the_reported_error(env):
    s, l, g, fake, broker, _ = env
    _listing(fake, transient=("twap", "iceberg"), code="51054")
    with pytest.raises(OKXAlgoCoverageUnverified) as ei:
        broker.preflight()
    assert {t for t, _c in ei.value.unverified} == {"twap", "iceberg"}
    assert all(c == "51054" for _t, c in ei.value.unverified)


def test_the_counter_example_a_blocked_type_is_never_read_as_empty(env):
    # An untriggered order exists whose type the listing refuses to answer for.
    # Coverage must NOT be reported as verified, and the run must stop: nothing
    # else in the API is documented to expose untriggered algo orders, so their
    # absence cannot be established from here.
    s, l, g, fake, broker, _ = env
    fake.untriggered_algos = [{"algoId": "hidden", "ordType": "trigger"}]
    fake.algo_pending_transient = ("trigger",)
    with pytest.raises(OKXAlgoCoverageUnverified) as ei:
        broker.preflight()
    assert [t for t, _c in ei.value.unverified] == ["trigger"]
    assert l.get("demo_initialized") is None


def test_an_untriggered_order_is_detected_where_the_listing_answers(env):
    s, l, g, fake, broker, _ = env
    fake.untriggered_algos = [{"algoId": "a", "ordType": "move_order_stop"}]
    with pytest.raises(RuntimeError, match="algo"):
        broker.preflight()


def test_the_history_effective_view_is_not_a_substitute(env):
    # `orders-algo-history` filters on `effective` (已生效, "has already taken
    # effect") / `canceled` / `order_failed`; the untriggered states (`live`,
    # `pause`) are neither filterable nor returned there, so an empty result from
    # it says nothing about untriggered orders. This adapter therefore has no such
    # call: re-adding one as "coverage" is the mistake this test guards against.
    import stonkfly.okx_broker as broker_module
    from stonkfly.okx_client import OKXClient

    assert not hasattr(OKXClient, "orders_algo_history")
    assert not hasattr(broker_module, "_ALGO_STATE_EFFECTIVE")
    s, l, g, fake, broker, _ = env
    fake.untriggered_algos = [{"algoId": "hidden", "ordType": "iceberg"}]
    fake.algo_pending_transient = ("iceberg",)
    with pytest.raises(OKXAlgoCoverageUnverified):
        broker.preflight()


def test_a_legacy_gap_ack_does_not_authorize_anything(env):
    # Directories written by the retired mechanism carry an
    # ``algo_coverage_gap_ack``. It is history, not permission: coverage must
    # still be obtained, and a failure must still stop the run.
    s, l, g, fake, broker, _ = env
    l.put("algo_coverage_gap_ack", sorted(set(ALGO_TYPES) | {"chase"}))
    _listing(fake, transient=("trigger",))
    with pytest.raises(OKXAlgoCoverageUnverified):
        broker.preflight()


def test_transport_failure_on_coverage_is_a_hard_stop(env):
    s, l, g, fake, broker, _ = env

    def boom(ord_type, retries=3):
        raise OKXTransportError("OKX HTTP error", status=502)

    fake.orders_algo_pending = boom
    with pytest.raises(OKXTransportError):
        broker.preflight()


def test_chase_is_covered_outside_spot_mode(env):
    s, l, g, fake, broker, _ = env
    asked = _listing(fake)
    broker.preflight()
    assert "chase" in asked  # acctLv 2 permits futures/swap instruments


def test_chase_is_not_covered_in_spot_mode(tmp_path):
    # ``chase`` is documented as FUTURES/SWAP-only, which is a reliable
    # documented reason for it to be inapplicable to acctLv 1 (Spot mode).
    s = Settings(products=("BTC-USDT",))
    l = Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    fake = FakeOKXAccount()
    fake.acct.update(acctLv="1")
    broker = OKXBroker(s, l, fake)
    asked = _listing(fake)
    broker.preflight()
    assert set(asked) == set(ALGO_TYPES)
    l.close()


# ---------------------------------------------------------------------------
# No private data in any output.
# ---------------------------------------------------------------------------

def test_preflight_result_carries_no_account_identifier(env):
    s, l, g, fake, broker, _ = env
    result = broker.preflight()
    blob = json.dumps(result)
    assert "uid-1" not in blob
    assert "account" not in result


def test_risk_error_never_echoes_a_balance(env):
    s, l, g, fake, broker, _ = env
    fake.extra_detail = {"liab": "1234.5678"}
    with pytest.raises(RuntimeError) as ei:
        broker.preflight()
    message = str(ei.value)
    assert "1234.5678" not in message  # the amount is never echoed
    assert "liab" in message           # the category is named


def _load_tool(name):
    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", name
    )
    spec = importlib.util.spec_from_file_location(name[:-3], script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_diagnostic_report_never_echoes_exchange_text(capsys):
    diagnose = _load_tool("okx_diagnose.py")
    diagnose.report(OKXRiskError("Non-zero risk field liab for USDT 1234.5678 hunter2"))
    out = capsys.readouterr().out
    assert "account_risk_not_verified" in out
    assert "1234.5678" not in out
    assert "hunter2" not in out


def test_diagnostic_reports_transport_status_not_a_body(capsys):
    diagnose = _load_tool("okx_diagnose.py")
    diagnose.report(OKXTransportError("OKX HTTP error", status=502))
    out = capsys.readouterr().out
    assert "transport_error" in out
    assert "502" in out


def test_status_command_does_not_print_the_account_identifier(tmp_path, capsys, monkeypatch):
    from stonkfly import cli

    s = Settings(products=("BTC-USDT",))
    l = Ledger(
        tmp_path / "ledger.sqlite", s, "okx-demo",
        identity={
            "exchange": "okx", "environment": "okx-demo",
            "account": "uid-1", "quote_ccy": "USDT",
        },
    )
    l.set_attempt_limit(2)
    l.close()
    monkeypatch.setattr(sys, "argv", ["stonkfly", "status", "--out", str(tmp_path)])
    cli.main()
    out = capsys.readouterr().out
    assert "uid-1" not in out
    assert '"account_bound": true' in out
    assert '"attempts_remaining": 2' in out


def test_a_probe_only_directory_is_not_left_halted(tmp_path, capsys, monkeypatch):
    # A failed preflight on a directory that has done no work must not leave a
    # permanent halt: the operator's next step is to fix configuration and
    # re-run in the same directory, not to hunt for a halt to clear.
    from stonkfly import cli

    monkeypatch.setattr(
        sys, "argv",
        [
            "stonkfly", "run", "--okx", "--okx-demo", "--preflight-only",
            "--out", str(tmp_path), "--max-order-attempts", "2",
        ],
    )
    # Force the broker's preflight to fail the way a timeout would.
    import stonkfly.okx_broker as broker_mod

    def boom(self):
        raise OKXRiskError("Untriggered algo coverage unavailable (ordType=trigger)")

    monkeypatch.setattr(broker_mod.OKXBroker, "preflight", boom)
    with pytest.raises(SystemExit):
        cli.main()
    reopened = Ledger(tmp_path / "ledger.sqlite", Settings(products=("BTC-USDT",)), "okx-demo")
    # Nothing was allocated, attempted or halted.
    assert reopened.get("halted") is None
    assert reopened.get("baseline") is None
    assert reopened.attempts_used() == 0
    assert reopened.get("attempt_limit") == 2
    reopened.close()


def test_a_stop_that_lands_mid_tick_is_a_clean_stop_not_a_halt(tmp_path, capsys, monkeypatch, lightweight_controller):
    """STOP is the documented way to stop a run, so it must never leave a halt.

    The loop checks STOP at the top of each tick, but the operator's file can
    arrive after that check has passed -- the risk guard is then what refuses.
    Nothing is submitted either way (the guard raises before any order), so that
    refusal is a requested stop, not an execution failure. Recording it as a
    halt made the documented "drop STOP and re-run" flow silently require
    --resume-reviewed.
    """
    from stonkfly import cli
    import stonkfly.risk as risk_mod

    monkeypatch.setattr(
        sys, "argv",
        ["stonkfly", "run", "--fixture", "--fast", "--out", str(tmp_path), "--steps", "3"],
    )
    real_check = risk_mod.Guard.check
    checks = []

    def check_after_stop_lands(self, quotes, now):
        # Let one tick commit first (so the directory has done work, exactly as
        # in the incident), then emulate the operator dropping STOP inside the
        # next tick, after the loop's own check has already passed.
        checks.append(None)
        if len(checks) > 1:
            self.stop_file.touch()
        return real_check(self, quotes, now)

    monkeypatch.setattr(risk_mod.Guard, "check", check_after_stop_lands)
    cli.main()

    assert "STOP file exists" in capsys.readouterr().err
    db = sqlite3.connect(f"file:{tmp_path / 'ledger.sqlite'}?mode=ro", uri=True)
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
    db.close()
    assert meta.get("tick") == 1  # one tick committed, then the stop landed
    assert meta.get("halted") is None  # a requested stop is not a failure


def test_error_json_records_the_exchange_code_and_no_body(tmp_path, monkeypatch, demo_credentials):
    """An ambiguous submission is diagnosable only through OKX's own code.

    The code is public API vocabulary (the same kind of value already recorded
    for an algo-coverage gap), so it is written to error.json; the response body,
    balances and identifiers are not.
    """
    from stonkfly import cli
    import stonkfly.okx_broker as broker_mod
    from stonkfly.broker import UnresolvedOrder

    monkeypatch.setattr(
        sys, "argv",
        [
            "stonkfly", "run", "--okx", "--okx-demo", "--preflight-only",
            "--out", str(tmp_path), "--max-order-attempts", "0",
        ],
    )

    def boom(self):
        raise UnresolvedOrder(
            "Order response was not a definite acceptance", okx_code="50013"
        )

    monkeypatch.setattr(broker_mod.OKXBroker, "preflight", boom)
    with pytest.raises(SystemExit):
        cli.main()
    diagnostic = json.loads((tmp_path / "error.json").read_text())
    assert diagnostic["okx_code"] == "50013"
    assert diagnostic["type"] == "UnresolvedOrder"
    # Nothing else from the exchange travels with the diagnostic.
    assert set(diagnostic) == {"type", "reason", "locations", "okx_code"}


def test_status_reports_an_adjudication(tmp_path, capsys, monkeypatch):
    # Recovery has to be inspectable through the same persistent ledger.
    from stonkfly import cli

    s = Settings(products=("BTC-USDT",))
    l = Ledger(
        tmp_path / "ledger.sqlite", s, "okx-demo",
        identity={
            "exchange": "okx", "environment": "okx-demo",
            "account": "uid-1", "quote_ccy": "USDT",
        },
    )
    l.put("order_attempts", 1)
    l.db.execute(
        "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
        ("c" * 32, "UNKNOWN", time.time(), json.dumps({
            "product": "BTC-USDT", "order_type": "limit_limit_fok",
            "side": "SELL", "base_size": "1", "limit_price": "1",
        })),
    )
    l.adjudicate_absent("c" * 32, "no match anywhere; intent age 0.10h")
    l.close()
    monkeypatch.setattr(sys, "argv", ["stonkfly", "status", "--out", str(tmp_path)])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    record = out["resolved_orders"][0]
    assert record["id"] == "c" * 32
    assert record["source"] == "operator"
    assert record["confirmation"] == "human_adjudication_not_exchange_confirmation"
    assert out["unresolved_orders"] == 0


def test_migrate_protocol_with_preflight_only_stops_before_trading(
    tmp_path, capsys, monkeypatch, lightweight_controller
):
    # The documented migration command pairs --migrate-protocol with
    # --preflight-only. That combination must record the migration and STOP:
    # falling through would start trading from a command that reads as a check.
    # Every entry point into the run loop is booby-trapped, so a regression
    # fails this test instead of quietly starting a worker.
    from stonkfly import cli
    import stonkfly.data as data_mod
    import stonkfly.okx_broker as broker_mod

    out = tmp_path / "m"
    out.mkdir()
    s = Settings(products=("BTC-USDT",))
    l = Ledger(out / "ledger.sqlite", s, "okx-demo")
    l.put("provenance_sha256", "old-source-signature")
    l.close()

    monkeypatch.setenv("OKX_API_KEY", "K")
    monkeypatch.setenv("OKX_API_SECRET", "S")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "P")
    monkeypatch.delenv("OKX_LIVE", raising=False)
    monkeypatch.setattr(
        sys, "argv",
        [
            "stonkfly", "run", "--okx", "--okx-demo", "--preflight-only",
            "--migrate-protocol", "--out", str(out),
        ],
    )

    calls = []

    def refuse(_self):
        calls.append(1)
        raise AssertionError("the trading loop started in preflight-only mode")

    monkeypatch.setattr(broker_mod.OKXBroker, "preflight", lambda self: {"mode": "okx-demo"})
    monkeypatch.setattr(broker_mod.OKXBroker, "reconcile", refuse)
    monkeypatch.setattr(broker_mod.OKXBroker, "verify_balances", refuse)
    # Preflight also calibrates the clock against OKX public time; keep the
    # test offline with a neutral offset.
    import stonkfly.okx_client as client_mod

    monkeypatch.setattr(
        client_mod.OKXClient,
        "measure_time_offset",
        lambda self, samples=5, local_clock=None, mono=None: {
            "offset": 0.05, "rtt": 0.1, "samples": [(0.1, 0.05)] * int(samples)
        },
    )
    monkeypatch.setattr(data_mod, "verify", lambda: {"ok": True})

    cli.main()  # returns; does not raise SystemExit

    out_json = capsys.readouterr().out
    assert '"mode": "okx-demo"' in out_json
    assert '"protocol_migrated": true' in out_json
    reopened = Ledger(out / "ledger.sqlite", s, "okx-demo")
    try:
        trail = reopened.get("protocol_migrations")
        assert len(trail) == 1 and trail[0]["from"] == "old-source-signature"
        assert reopened.get("provenance_sha256") != "old-source-signature"
        assert reopened.get("halted") is None
        assert reopened.get("tick") == 0
        assert reopened.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert not (out / "events.jsonl").exists()
    finally:
        reopened.close()


def test_sell_limit_stays_inside_okx_price_band(tmp_path):
    # OKX rejects a sell priced below its dynamic price band (51138, "The lowest
    # price limit for sell orders is {param0}"); the band sits at about -0.5%
    # from last and bid <= last, so the old bid*(1-0.005) limit landed on or
    # under the line every time (7 of 7 rejections). The sell buffer is capped
    # inside the band; the buy buffer keeps its full width because buys pass by
    # construction (rounding up keeps ask*(1+x) above the mirror limit 51137).
    from stonkfly.config import D as _D
    from stonkfly.risk import Guard

    s = Settings(products=("BTC-USDT",))
    l = Ledger(tmp_path / "l.sqlite", s, "okx-demo")
    g = Guard(s, l, tmp_path / "STOP")
    bid, ask = _D("81060"), _D("81060.1")
    q = Quote(
        "BTC-USDT", bid, ask, time.time(),
        _D("0.00000001"), None, _D("0.1"), None, _D("0.00001"),
    )
    l.put("positions", {"BTC-USDT": "1"})
    sell = g.plan("BTC-USDT", "SELL", {"BTC-USDT": q})
    buy = g.plan("BTC-USDT", "BUY", {"BTC-USDT": q})
    # Sell: 0.2% below the bid, rounded down to the 0.1 tick.
    # 81060 * 0.998 = 80897.88 -> down -> 80897.8, i.e. ~0.2% inside the -0.5% band.
    assert sell["limit_price"] == "80897.8"
    # Buy: unchanged -- 0.5% above the ask, rounded up to the 0.1 tick.
    # 81060.1 * 1.005 = 81465.4005 -> up -> 81465.5.
    assert buy["limit_price"] == "81465.5"
    l.close()
