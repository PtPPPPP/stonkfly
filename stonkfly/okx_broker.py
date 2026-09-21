"""OKX demo-trading execution: cash spot, price-bounded FOK only, with a
budget carved out of an otherwise untouched account.

Live OKX execution is explicitly not supported in this build. Demo requests
carry the ``x-simulated-trading: 1`` header and use demo credentials. Order
placement is a single submission with no retry; ambiguous outcomes stop the
worker for reconciliation instead of being resubmitted.

Account model (acctLv 1 or 2, cash spot only):
- Current OKX documentation defines ``acctLv`` 1 as Spot mode and 2 as Futures
  mode (3 = Multi-currency margin, 4 = Portfolio margin). Both 1 and 2 accept
  ``tdMode=cash`` SPOT orders, so either may run this adapter -- but only while
  the account provably holds no margin, derivative or borrow exposure, which is
  re-verified every tick. Levels 3 and 4 are rejected outright.
- The demo account is not assumed to be empty. At first init a snapshot of its
  actual cash balances (``baseline``) is persisted, and the bot is granted a
  virtual ``budget`` (default 100 quote units) drawn from the account's quote
  currency. The bot may only spend its own budget and may only sell base units
  it bought itself; gifted BTC/ETH/OKB and the remaining quote balance are
  unallocated and must not move.
- Reconciliation compares the exchange cash balance against
  ``baseline + (cash - budget)`` for the quote and ``baseline + positions`` for
  each base, so a deposit/withdrawal/other-robot trade is a hard error, never
  treated as profit. This is an in-program budget, not exchange-level fund
  isolation; the account must not be shared while the bot runs.

Balance details are read against a documented field model rather than "any
non-zero field is bad": required balances must be present and non-negative,
documented risk indicators must be present and zero (with ``""`` -- OKX's
documented "not applicable under this account level" encoding -- accepted),
quantitative fields such as ``maxLoan`` (a borrowable *capacity*) and
``mgnRatio`` (a *ratio*) are range-checked but not forced to zero, and a field
that is simply absent is a hard stop because the risk cannot then be verified.

Untriggered algo/strategy orders are queried for every ordType OKX documents as
applicable to this account mode. OKX persistently answers ``51054`` ("Request
timed out. Please try again.") for several of them and documents no alternative
listing endpoint, so their absence cannot be verified: that is reported as an
explicit coverage gap and stops the run unless the operator acknowledges
exactly those types for a bounded acceptance run.

Intent is persisted (``PREPARED``) by the ledger before submission, and the
attempt budget is spent atomically with the ``UNKNOWN`` transition that precedes
any request leaving the process. Fees are handled per currency and sign (a
rebate is never abs'd into a charge).
"""

import os
import time

from .broker import UnresolvedOrder
from .config import D
from .okx_client import (
    OKXBusinessError,
    OKXTransportError,
    demo_client_from_env,
)

TERMINAL = {"filled", "canceled", "mmp_canceled"}

# Account modes this build may run: 1 = Spot mode, 2 = Futures mode. Both accept
# tdMode=cash SPOT orders; margin/derivative exposure is ruled out separately.
_CASH_SPOT_LEVELS = ("1", "2")

# Borrowing-related account settings. A cash-only build must be unable to
# borrow, so these must be present (a missing setting cannot be verified) and
# must be off.
_BORROW_SETTINGS = ("enableSpotBorrow", "autoLoan", "spotBorrowAutoRepay")

# --- OKX balance-detail field model ---------------------------------------
# Reference: GET /api/v5/account/balance, the per-field descriptions and the
# "Distribution of applicable fields under each account level" matrix. Verified
# against a live read-only sample of this demo account (2026-09-16).
#
# _REQUIRED_BALANCE: the money fields the bot allocates and spends from. They
# must be present, parseable and non-negative -- a missing or empty balance is
# never defaulted to zero.
_REQUIRED_BALANCE = ("cashBal", "availBal")
# _MUST_BE_ZERO: documented risk / fund-commitment indicators. A present zero or
# a present "" ("not applicable") is clear; a non-zero value is a liability,
# borrow, margin occupation, frozen balance, bot allocation or forced-repayment
# risk. An absent field is a hard stop: the risk would be unverifiable.
_MUST_BE_ZERO = (
    "frozenBal", "ordFrozen", "liab", "crossLiab", "isoLiab", "interest",
    "upl", "uplLiab", "isoEq", "isoUpl", "imr", "mmr", "notionalLever",
    "twap", "frpType", "stgyEq", "borrowFroz", "spotInUseAmt",
    "clSpotInUseAmt", "maxSpotInUse", "fixedBal", "autoLendAmt",
    "autoLendMtAmt",
)
# _NONNEGATIVE: quantitative fields whose magnitude is not itself a risk.
# ``maxLoan`` is the maximum *borrowable* amount and ``mgnRatio`` is a *ratio*;
# requiring either to be zero would be meaningless, so only presence-when-set
# and a sane range are enforced. Absence of these is not a coverage gap because
# a non-zero value cannot indicate exposure on its own.
_NONNEGATIVE = (
    "maxLoan", "mgnRatio", "eq", "eqUsd", "availEq", "disEq", "rewardBal",
    "colRes", "colBorrAutoConversion",
)
# Local features that move or lock funds off the account. Documented enum for
# autoLendStatus: unsupported / off / pending / active.
_LOCAL_STATE = {
    "autoLendStatus": ("", "unsupported", "off"),
    "autoStakingStatus": ("", "unsupported", "off"),
}

# ordType values accepted by GET /api/v5/trade/orders-algo-pending that can
# exist for a SPOT/cash account. ``chase`` is documented as FUTURES/SWAP-only,
# so it is only queried in account modes that permit those instruments -- the
# one type that carries a reliable documented reason to be inapplicable here.
_ALGO_TYPES = (
    "conditional", "oco", "trigger", "move_order_stop", "iceberg", "twap",
    "smart_iceberg",
)
_ALGO_TYPES_DERIVATIVE = ("chase",)
# Documented retention for orders canceled without any fill: "The incomplete
# orders that have been canceled are only reserved for 2 hours."
_CANCEL_UNFILLED_RETENTION_SECONDS = 2 * 3600
# Conservative evidence horizon for adjudicating an UNKNOWN intent as absent.
# OKX documents that fills stay in orders-history for 7 days and in the
# 3-month archive; beyond the archive window a real fill would appear in
# NEITHER scan, so "the order matches nothing" stops being evidence of
# absence. 85 days sits a safety margin inside the documented ~90-day window
# (docs/okx.md): past it the intent stays UNKNOWN and only a human with
# evidence from outside the retention windows may resolve it. Never guessed
# upward from this constant: it is deliberately shorter than what OKX
# publishes, not longer.
_EVIDENCE_HORIZON_SECONDS = 85 * 86400
# The one ordType pair OKX allows to be comma-combined, so the documented
# "untriggered" listing can be checked against the history view in one request.
_ALGO_CROSS_CHECK_TYPES = "conditional,oco"


class OKXAlgoCoverageUnverified(RuntimeError):
    """Untriggered algo orders could not be enumerated for some ordTypes.

    ``unverified`` holds ``(ordType, okx_code)`` pairs. It is reported so the
    operator can see exactly what could not be checked; it is not permission to
    proceed, and nothing in this build turns it into one.
    """

    def __init__(self, unverified):
        super().__init__(
            "Untriggered algo order coverage could not be obtained for "
            f"{', '.join(t for t, _c in unverified)}; no documented substitute "
            "exists, so the account risk state cannot be verified"
        )
        self.unverified = tuple(unverified)


class OKXRiskError(RuntimeError):
    """A risk precondition is violated, or cannot be verified.

    Messages are stable categories naming only field names, currencies,
    ordTypes and OKX business codes -- never a balance, an account id, a
    request body, a key or a signature.
    """


class OKXBroker:
    mode = "okx-demo"
    exchange = "okx"

    def __init__(self, settings, ledger, client):
        # OKX live execution is unsupported: a non-demo client would send
        # requests without ``x-simulated-trading`` and could touch a real
        # account. Reject it at construction, not only in ``from_env``.
        if not getattr(client, "demo", False):
            raise RuntimeError("OKXBroker requires a demo client (live is unsupported)")
        self.s = settings
        self.l = ledger
        self.client = client
        self.uid = None
        self._acct_lv = None
        self.algo_coverage = None

    @classmethod
    def from_env(cls, settings, ledger):
        if os.environ.get("OKX_LIVE") == "I_ACCEPT_REAL_TRADES":
            raise RuntimeError("OKX live execution is not supported in this build")
        return cls(settings, ledger, demo_client_from_env())

    # -- account state -----------------------------------------------------
    def _account_config(self):
        cfg = self.client.account_config()
        if not isinstance(cfg, dict) or not cfg:
            raise OKXRiskError("Account config unavailable")
        return cfg

    def _assert_account_mode(self, cfg):
        """Validate account mode, key permissions, identity and borrowing.

        Called at preflight and again on every balance verification, so a mode,
        identity or permission change mid-run stops the worker instead of being
        trusted forever after startup.
        """
        acct_lv = cfg.get("acctLv")
        if acct_lv not in _CASH_SPOT_LEVELS:
            raise OKXRiskError(
                "Require a spot or futures-mode account (acctLv 1 or 2)"
            )
        perms = {p.strip() for p in (cfg.get("perm") or "").split(",") if p.strip()}
        if "trade" not in perms:
            raise OKXRiskError("API key lacks trade permission")
        if "withdraw" in perms:
            raise OKXRiskError("API key must not carry withdraw permission")
        uid = cfg.get("uid")
        main_uid = cfg.get("mainUid")
        if not uid or not main_uid:
            raise OKXRiskError("Account identity missing")
        if uid != main_uid:
            raise OKXRiskError("Sub-account is not supported; use the main account")
        if cfg.get("type") != "0":
            raise OKXRiskError("Require a main trading account")
        if self._acct_lv is not None and acct_lv != self._acct_lv:
            raise OKXRiskError("Account mode changed during the run")
        self._acct_lv = acct_lv
        for key in _BORROW_SETTINGS:
            value = cfg.get(key)
            if not isinstance(value, bool):
                raise OKXRiskError(f"Borrow setting {key} absent from account config")
            if value:
                raise OKXRiskError(
                    f"Borrow setting {key} is enabled; a cash-only build needs it off"
                )
        return acct_lv

    def _details(self):
        """Full-coverage balance details: {ccy: detail} for every currency."""
        data = self.client.balance()  # no ccy -> every non-zero balance
        if not data:
            raise OKXRiskError("Balance data missing")
        details = data[0].get("details")
        if not isinstance(details, list) or not details:
            raise OKXRiskError("Balance details missing")
        out = {}
        for d in details:
            ccy = d.get("ccy")
            if not ccy:
                raise OKXRiskError("Balance detail lacks a currency")
            out[ccy] = d
        return out

    @staticmethod
    def _money(detail, field, ccy):
        """A required money field: present, parseable, never defaulted."""
        if field not in detail:
            raise OKXRiskError(f"Balance field {field} absent for {ccy}")
        raw = detail[field]
        if raw is None or raw == "":
            raise OKXRiskError(f"Balance field {field} empty for {ccy}")
        try:
            return D(raw)
        except Exception:
            raise OKXRiskError(f"Unparseable balance field {field} for {ccy}") from None

    def _cash_balances(self, details):
        """Actual cash balance per currency (the reconciliation basis)."""
        return {ccy: self._money(d, "cashBal", ccy) for ccy, d in details.items()}

    def _avail_balances(self, details):
        """Actually spendable balance per currency."""
        return {ccy: self._money(d, "availBal", ccy) for ccy, d in details.items()}

    def _check_balance_fields(self, details):
        for ccy, d in details.items():
            for field in _REQUIRED_BALANCE:
                if self._money(d, field, ccy) < 0:
                    raise OKXRiskError(f"Negative {field} for {ccy}")
            for field in _MUST_BE_ZERO:
                if field not in d:
                    raise OKXRiskError(
                        f"Account risk field {field} absent for {ccy}; the risk "
                        "cannot be verified from this balance snapshot"
                    )
                raw = d[field]
                if raw is None or raw == "":
                    continue  # documented "not applicable under this account level"
                try:
                    value = D(raw)
                except Exception:
                    raise OKXRiskError(
                        f"Unparseable risk field {field} for {ccy}"
                    ) from None
                if value != 0:
                    raise OKXRiskError(f"Non-zero risk field {field} for {ccy}")
            for field in _NONNEGATIVE:
                raw = d.get(field)
                if raw is None or raw == "":
                    continue
                try:
                    value = D(raw)
                except Exception:
                    raise OKXRiskError(f"Unparseable field {field} for {ccy}") from None
                if value < 0:
                    raise OKXRiskError(f"Negative field {field} for {ccy}")
            for field, allowed in _LOCAL_STATE.items():
                raw = d.get(field)
                if raw is None or raw == "":
                    continue
                if str(raw) not in allowed:
                    raise OKXRiskError(f"Local feature {field} is active for {ccy}")

    def _check_no_positions(self):
        if self.client.positions():
            raise OKXRiskError("Derivative/margin positions present")

    def _check_account_position_risk(self):
        """The risk snapshot that exists for this account mode.

        ``/account/risk-state`` is documented as Portfolio-margin-only and
        answers 51010 for this account, so the applicable risk view is
        ``/account/account-position-risk``. A missing field is a stop, not a
        silent "no risk".
        """
        for inst_type in ("MARGIN", "SWAP", "FUTURES"):
            snap = self.client.account_position_risk(inst_type)
            positions = snap.get("posData")
            if not isinstance(positions, list):
                raise OKXRiskError(f"{inst_type} risk snapshot lacks a position list")
            if positions:
                raise OKXRiskError(f"Open {inst_type} risk position present")
            if "adjEq" not in snap:
                raise OKXRiskError(f"{inst_type} risk snapshot lacks adjusted equity")
            if snap["adjEq"] not in (None, ""):
                raise OKXRiskError(
                    "Adjusted equity reported; this account is not cash-only"
                )

    def _check_no_open_orders(self):
        if self.client.orders_pending():  # no instId -> all instruments
            raise OKXRiskError("External/open order in demo account")

    def _check_no_algo_orders(self, acct_lv, full=True):
        """Enumerate untriggered algo orders, or refuse to proceed.

        The only documented view of an *untriggered* algo order is
        ``GET /api/v5/trade/orders-algo-pending``: "Retrieve a list of untriggered
        Algo orders under the current account", whose returned states are ``live``
        ("待生效", awaiting effect) and ``pause`` ("暂停生效", effect suspended).

        There is no substitute when it refuses to answer. ``GET
        /api/v5/trade/orders-algo-history`` filters on ``effective`` ("已生效",
        already in effect), ``canceled`` and ``order_failed``; the untriggered
        states are neither filterable nor returned there, so an empty history
        result carries no information about untriggered orders. An earlier version
        of this adapter read ``effective`` as "not yet triggered" and used the
        history view as coverage -- the Chinese state names show the opposite, and
        the documentation's own example of an ``effective`` order carries a
        populated trigger time and spawned order id.

        So a definitive ``code=0`` list is required per applicable ordType. A
        timeout (OKX answers ``51054`` for several types here), an unsupported
        code, or any other failure leaves that type unverified and stops the run.
        There is deliberately no acknowledgement path: "unverified" is a stop, not
        a setting.

        ``full=False`` is the fast send-boundary variant, which checks only the
        comma-combinable pair (the full sweep runs on every tick).
        """
        verified = []
        unverified = []
        types = self._algo_types(acct_lv) if full else (_ALGO_CROSS_CHECK_TYPES,)
        for ord_type in types:
            try:
                orders = self.client.orders_algo_pending(ord_type)
            except OKXBusinessError as e:
                unverified.append((ord_type, e.code))
                continue
            if orders:
                raise OKXRiskError(f"Untriggered algo order present (ordType={ord_type})")
            verified.append(ord_type)
        if unverified:
            raise OKXAlgoCoverageUnverified(unverified)
        return {
            "source": "orders-algo-pending (documented untriggered listing)",
            "verified": verified,
            "unverified": [],
        }

    @staticmethod
    def _algo_types(acct_lv):
        return _ALGO_TYPES + (() if acct_lv == "1" else _ALGO_TYPES_DERIVATIVE)

    def _check_risk(self, details, acct_lv):
        self._check_balance_fields(details)
        self._check_no_positions()
        self._check_account_position_risk()
        self._check_no_open_orders()
        # Recorded so preflight can report what coverage was actually obtained.
        self.algo_coverage = self._check_no_algo_orders(acct_lv)

    def preflight(self):
        cfg = self._account_config()
        acct_lv = self._assert_account_mode(cfg)
        uid = cfg["uid"]
        self.uid = uid
        # Bind (or verify) the account identity BEFORE reconcile: on mismatch
        # this raises without touching any order state, balances or demo
        # initialization.
        self.l.bind_account(uid)
        # Reconcile the bot's own pending intents before checking for external
        # orders, so an in-flight order of ours is settled rather than mistaken
        # for external activity.
        self.reconcile()
        if not self.l.get("demo_initialized"):
            details = self._details()
            self._check_risk(details, acct_lv)
            self._init_budget(details)
        self.verify_balances()
        return {
            "mode": "okx-demo",
            "account_bound": True,
            "spot_cash_mode": True,
            "withdrawals_disabled": True,
            "account_level": acct_lv,
            "quote_ccy": self.s.quote_ccy,
            "order_attempts": self.l.attempts_used(),
            "attempt_limit": self.l.get("attempt_limit"),
            "algo_coverage": "verified",
            "algo_coverage_source": self.algo_coverage["source"],
            "algo_coverage_types_verified": self.algo_coverage["verified"],
        }

    def _init_budget(self, details):
        quote = self.s.quote_ccy
        if (
            self.l.get("tick")
            or self.l.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        ):
            raise OKXRiskError("Uninitialized demo ledger already has activity")
        budget = D(self.s.capital)
        cash = self._cash_balances(details)
        avail = self._avail_balances(details)
        if cash.get(quote, D(0)) < budget:
            raise OKXRiskError(
                f"Insufficient {quote} cash balance for the {budget} budget"
            )
        # A balance can be present yet wholly unavailable (frozen, order-frozen,
        # lent out). The budget must be spendable, not merely displayed.
        if avail.get(quote, D(0)) < budget:
            raise OKXRiskError(
                f"Insufficient available {quote} balance for the {budget} budget"
            )
        baseline = {ccy: str(v) for ccy, v in cash.items()}
        with self.l.transaction():
            self.l.put("baseline", baseline)
            self.l.put("budget", str(budget))
            for k in ["cash", "initial_cash", "anchor"]:
                self.l.put(k, str(budget))
            self.l.put("demo_initialized", True)

    def verify_balances(self):
        """Full re-verification: account mode, identity, risk set and balances.

        Runs on every tick and at preflight, so nothing verified at startup is
        trusted permanently.
        """
        cfg = self._account_config()
        acct_lv = self._assert_account_mode(cfg)
        self.l.check_account(cfg["uid"])
        details = self._details()
        self._check_risk(details, acct_lv)
        self._check_available_covers_budget(details)
        self._reconcile_balances(details)
        return details

    def _check_available_covers_budget(self, details):
        quote = self.s.quote_ccy
        if self._avail_balances(details).get(quote, D(0)) < self.l.cash:
            raise OKXRiskError(
                f"Available {quote} no longer covers the bot's remaining budget"
            )

    def _verify_before_send(self):
        """The send-boundary re-verification.

        Deliberately narrower and faster than ``verify_balances``: every OKX
        request costs about a second, and the plan has to be sent inside the
        quote-age window, so the boundary re-checks what can invalidate *this*
        send -- account mode, identity, the funds it needs, and the resting
        orders OKX will list -- rather than repeating the whole tick sweep. The
        full sweep (including every untriggered algo ordType) runs each tick, and
        an untriggered algo order cannot move funds until it fires, at which
        point the balance reconciliation below stops this send.
        """
        cfg = self._account_config()
        acct_lv = self._assert_account_mode(cfg)
        self.l.check_account(cfg["uid"])
        details = self._details()
        self._check_balance_fields(details)
        self._check_available_covers_budget(details)
        self._reconcile_balances(details)
        self._check_no_open_orders()
        self._check_no_algo_orders(acct_lv, full=False)
        return details

    def _check_available_for(self, plan, details):
        """The spendable balance must cover this specific order."""
        quote = self.s.quote_ccy
        avail = self._avail_balances(details)
        size = D(plan["base_size"])
        if plan["side"] == "BUY":
            needed = size * D(plan["limit_price"]) + D(plan["fee_ceiling"])
            if avail.get(quote, D(0)) < needed:
                raise OKXRiskError(
                    f"Available {quote} does not cover this order and its fee ceiling"
                )
        else:
            base = plan["product"].split("-")[0]
            if avail.get(base, D(0)) < size:
                raise OKXRiskError(f"Available {base} does not cover this order")

    def _reconcile_balances(self, details):
        baseline = self.l.get("baseline")
        budget = self.l.get("budget")
        if not baseline or budget is None:
            raise OKXRiskError("Ledger lacks account baseline/budget")
        quote = self.s.quote_ccy
        budget = D(budget)
        # Expected exchange cash = baseline + the bot's net contribution.
        # Quote currency moves by (cash - budget); each base currency moves by
        # the bot's held position in it. Unallocated assets must stay put.
        expected = {ccy: D(v) for ccy, v in baseline.items()}
        expected[quote] = expected.get(quote, D(0)) + (self.l.cash - budget)
        for product, amount in self.l.positions.items():
            base = product.split("-")[0]
            expected[base] = expected.get(base, D(0)) + amount
        actual = self._cash_balances(details)
        for ccy in set(expected) | set(actual):
            tol = D("0.0001") if ccy == quote else D("0.00000001")
            if abs(actual.get(ccy, D(0)) - expected.get(ccy, D(0))) > tol:
                raise OKXRiskError(
                    "External balance change; stop and reconcile rather than treat deposits as profit"
                )

    # -- execution ---------------------------------------------------------
    def execute(self, plan, before_submit):
        cid = plan["client_order_id"]
        try:
            # Re-verify account mode, identity, balances and resting orders at the
            # send boundary, then confirm this order is fundable.
            details = self._verify_before_send()
            self._check_available_for(plan, details)
            before_submit(plan)
            # Durable attempt spend + UNKNOWN transition, atomically, before any
            # request that can place an order. A crash here over-counts.
            self.l.begin_attempt(cid)
        except Exception:
            # Nothing has been sent yet, so the intent is abandoned rather than
            # left unresolved -- including when the attempt budget is exhausted.
            self.l.mark(cid, "REJECTED")
            raise
        payload = {
            "instId": plan["product"],
            "tdMode": "cash",
            "side": plan["side"].lower(),
            "ordType": "fok",
            "sz": plan["base_size"],
            "px": plan["limit_price"],
            "clOrdId": cid,
            # OKX's price limit is dynamic and deliberately undisclosed ("based
            # on more than a dozen parameters... the full set of rules is not
            # completely disclosed" -- the price-limit help page). A px outside
            # the band is otherwise rejected (our 51138 sells); with the
            # documented amendment the exchange moves the price to the best
            # available value inside the band instead. For a sell that can only
            # raise the floor (never sells below the planned limit), and the FOK
            # still fills at the market bid or cancels unharmed.
            "pxAmendType": "1",
        }
        try:
            resp = self.client.place_order(payload)
        except OKXTransportError as e:
            raise UnresolvedOrder(
                "Submission outcome unknown; reconcile before any further trade"
            ) from e
        # Envelope validation. The documented rule (General Information: "It is
        # sCode and sMsg that represent the request result or error reason when
        # the return data has sCode rather than code and msg") makes a present
        # per-order sCode the authoritative result for THIS order: the top-level
        # code only grades the request envelope ("0" ok, "1" failed, "2"
        # partially succeeded). So an envelope-level failure that still carries a
        # per-order sCode is a definite answer, and reading it as ambiguous would
        # turn an answered rejection into an unresolved halt. Anything without a
        # usable sCode (a timeout or busy envelope carries none) stays UNKNOWN.
        if not isinstance(resp, dict):
            raise UnresolvedOrder("Order response missing or malformed envelope")
        code = resp.get("code")
        if code is None or code == "":
            raise UnresolvedOrder("Order response lacked a result code")
        data = resp.get("data")
        item = (
            data[0]
            if isinstance(data, list) and data and isinstance(data[0], dict)
            else None
        )
        if item is None:
            raise UnresolvedOrder("Order response lacked order data", okx_code=code)
        scode = item.get("sCode")
        if scode is None or scode == "":
            # No per-order answer at all, so the outcome of this order is unknown
            # whatever the envelope says.
            raise UnresolvedOrder("Order data item lacked a result code", okx_code=code)
        if scode != "0":
            # Definite per-order rejection, whatever the envelope's code was. The
            # codes are recorded because they name the exchange's own reason --
            # public API vocabulary, never account data.
            self.l.mark(cid, "REJECTED")
            # sMsg is a fixed message template (sometimes with a public bound such
            # as the price-limit value in 51138) -- exchange vocabulary, never
            # account data, and it is what names the exact reason.
            return {
                "status": "REJECTED",
                "mode": "okx-demo",
                "okx_code": code,
                "order_scode": scode,
                "order_smsg": item.get("sMsg") or "",
            }
        oid = item.get("ordId")
        if not oid:
            raise UnresolvedOrder(
                "Accepted order lacked an unambiguous order id", okx_code=code
            )
        self.l.mark(cid, "ACCEPTED", oid)
        deadline = time.monotonic() + 30
        while True:
            if self._settle(cid, oid, plan):
                return {"mode": "okx-demo", "status": "SETTLED", "client_order_id": cid}
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        raise UnresolvedOrder("Order is not terminal; demo execution stopped")

    def _fee(self, order, product):
        base_ccy = product.split("-")[0]
        quote_ccy = product.split("-")[1]

        def map_ccy(ccy):
            if ccy == base_ccy:
                return "base"
            if ccy == quote_ccy:
                return "quote"
            return None

        # OKX sign convention: fee negative = paid; rebate positive = received.
        # A filled order must report its fee field; a missing/empty fee is
        # incomplete data, never a silent zero. A rebate of ""/missing is the
        # documented "no rebate".
        if "fee" not in order or order.get("fee") in (None, ""):
            raise UnresolvedOrder("Filled order lacks a fee field")
        fee = D(order["fee"])
        fee_ccy = order.get("feeCcy") or ""
        cost = D(0)  # positive = cost to us, negative = rebate credit
        cost_ccy = None
        if fee != 0:
            m = map_ccy(fee_ccy)
            if m is None:
                raise UnresolvedOrder("Unsupported or missing fee currency")
            cost = -fee
            cost_ccy = m
        rebate = order.get("rebate")
        if rebate in (None, ""):
            rebate = D(0)
        else:
            rebate = D(rebate)
            if rebate != 0:  # a zero rebate needs no currency
                rebate_ccy = order.get("rebateCcy") or ""
                m = map_ccy(rebate_ccy)
                if m is None:
                    raise UnresolvedOrder("Unsupported or missing rebate currency")
                if cost_ccy is None:
                    cost = -rebate
                    cost_ccy = m
                elif cost_ccy == m:
                    cost = cost - rebate
                else:
                    raise UnresolvedOrder("Split fee/rebate accounting unsupported")
        return cost, (cost_ccy or "quote")

    def _settle(self, cid, oid, plan):
        order = self.client.get_order(plan["product"], ord_id=oid)
        if order is None:
            raise UnresolvedOrder("Order not found; do not assume no fill")
        if (
            order.get("ordId") != oid
            or order.get("clOrdId") != cid
            or order.get("instId") != plan["product"]
            or order.get("side") != plan["side"].lower()
        ):
            raise UnresolvedOrder("Order identity mismatch")
        state = order.get("state")
        if state not in TERMINAL:
            return False
        # accFillSz is always present on a valid terminal order; a missing or
        # empty value is incomplete data, never a legitimate zero fill.
        if "accFillSz" not in order or order.get("accFillSz") in (None, ""):
            raise UnresolvedOrder("Terminal order lacks accumulated fill size")
        try:
            fill = D(order["accFillSz"])
        except Exception:
            raise UnresolvedOrder("Invalid accumulated fill size")
        if fill < 0:
            raise UnresolvedOrder("Negative accumulated fill size")
        if fill == 0:
            # Terminal with no fill (e.g. FOK cancelled unfilled): a legitimate zero.
            self.l.settle(cid, D(0), D(0), D(0), "quote")
            return True
        avg = order.get("avgPx")
        if not avg:
            raise UnresolvedOrder("Filled order lacks average fill price")
        base = fill
        quote = base * D(avg)
        cost, fee_ccy = self._fee(order, plan["product"])
        self.l.settle(cid, base, quote, cost, fee_ccy)
        return True

    # -- uncertain-intent investigation and adjudication -------------------
    def verify_account_and_balances(self):
        """Identity, mode, risk fields and balance reconciliation only.

        Unlike ``preflight`` this never touches order-intent state, so it can run
        while an UNKNOWN intent is still open. An adjudication calls it first: if
        any funds moved, reconciliation raises and the intent stays UNKNOWN.
        """
        cfg = self._account_config()
        self._assert_account_mode(cfg)
        self.l.check_account(cfg["uid"])
        details = self._details()
        self._check_balance_fields(details)
        self._reconcile_balances(details)
        return details

    def investigate_unknown(self, cid, plan, created):
        """See :func:`investigate_unknown`; bound to this broker's client.

        The product is passed on its own, never the whole plan: the module-level
        investigation hands it straight to the order lookup as ``instId``, and
        OKX rejects anything that is not a plain instrument string with 51001.
        """
        return investigate_unknown(self.client, cid, plan.get("product"), created)

    def adjudication_basis(self, plan, created, findings):
        """See :func:`adjudication_basis`."""
        return adjudication_basis(plan, created, findings)

    def adjudicate_absent(self, cid):
        """Operator-checked closure of one UNKNOWN intent.

        Order of operations is deliberate: identity and balance reconciliation
        come first, so a closure can only happen on an account that still matches
        the ledger; then the read-only evidence is collected and judged; only
        then is the status changed, atomically with its audit record. Any failure
        leaves the intent UNKNOWN, and the consumed attempt is never returned.
        """
        self.verify_account_and_balances()
        row = next((r for r in self.l.pending() if r["id"] == cid), None)
        if row is None:
            raise OKXRiskError("No uncertain intent with that client order id")
        if row["status"] != "UNKNOWN" or row["exchange_id"]:
            raise OKXRiskError(
                "Only an UNKNOWN intent without an exchange order id can be adjudicated"
            )
        findings = self.investigate_unknown(cid, row["plan"], row["created"])
        allowed, basis = self.adjudication_basis(row["plan"], row["created"], findings)
        if not allowed:
            raise OKXRiskError(f"Adjudication refused: {basis}")
        return self.l.adjudicate_absent(cid, basis)

    def reconcile(self):
        for row in self.l.pending():
            cid = row["id"]
            plan = row["plan"]
            if row["status"] == "PREPARED":
                self.l.mark(cid, "REJECTED")
                continue
            oid = row["exchange_id"]
            if not oid:
                order = self.client.get_order(plan["product"], cl_ord_id=cid)
                if order is None:
                    raise UnresolvedOrder(
                        "Uncertain submission not found. Check OKX demo; no automatic resubmission."
                    )
                oid = order.get("ordId")
                if not oid:
                    raise UnresolvedOrder("Found order lacks an id")
                self.l.mark(cid, "ACCEPTED", oid)
            if not self._settle(cid, oid, plan):
                raise UnresolvedOrder("Order still pending at OKX demo")


def investigate_unknown(client, client_order_id, product, created, now=None):
    """Collect, read-only, everything OKX will say about one intent.

    Module level and free of ledger state on purpose: the automated path and the
    read-only audit tool must reason from exactly the same evidence, and a tool
    must be able to gather it without opening (or being able to write) a ledger.

    Never concludes anything. It returns the raw findings plus the completeness
    and retention window of each source, so a human can judge how much the
    absence of a match is worth. ``complete`` is False whenever a scan stopped
    early, and a truncated scan is never presented as a full one.
    """
    now = time.time() if now is None else now
    findings = {
        "client_order_id": client_order_id,
        "investigated_at": now,
        "intent_created": created,
        "age_seconds": now - created,
        "lookup": None,
        "open_orders": [],
        "history": [],
        "history_rows": 0,
        "history_complete": False,
        "archive": [],
        "archive_rows": 0,
        "archive_complete": False,
    }
    findings["lookup"] = client.get_order(product, cl_ord_id=client_order_id)
    findings["open_orders"] = [
        o for o in client.orders_pending() if o.get("clOrdId") == client_order_id
    ]
    history, history_complete = client.orders_history("SPOT")
    findings["history_rows"] = len(history)
    findings["history_complete"] = history_complete
    findings["history"] = [o for o in history if o.get("clOrdId") == client_order_id]
    archive, archive_complete = client.orders_history_archive("SPOT")
    findings["archive_rows"] = len(archive)
    findings["archive_complete"] = archive_complete
    findings["archive"] = [o for o in archive if o.get("clOrdId") == client_order_id]
    return findings


def adjudication_basis(plan, created, findings):
    """Decide whether the evidence supports closing this intent, and say why.

    Two documented facts make this decidable instead of a matter of opinion:

    - This bot submits only ``fok`` orders, which OKX defines as all-or-nothing
      ("the order would not be partially filled"). An accepted order is therefore
      either fully filled or canceled with no fill, and a canceled-without-fill
      order moves no cash, position or fee.
    - ``orders-history`` keeps orders that were "canceled without any fills" for
      only 2 hours, while fills stay visible there for 7 days and up to 3 months
      in the archive.

    So the age of the intent decides what a clean miss rules out, and the basis
    string states which case applied rather than generalising.
    """
    visible = [
        name
        for name in ("lookup", "open_orders", "history", "archive")
        if findings[name]
    ]
    if visible:
        return False, f"the order is visible at OKX ({','.join(visible)})"
    if not (findings["history_complete"] and findings["archive_complete"]):
        return False, "the order-history scan did not complete"
    if plan.get("order_type") != "limit_limit_fok":
        return False, "the intent is not a fill-or-kill order"
    age = findings["age_seconds"]
    if age <= _CANCEL_UNFILLED_RETENTION_SECONDS:
        window = (
            "age <= 2h: a canceled-without-fill order would still be listed, so "
            "no fill and no resting order are both ruled out"
        )
    else:
        window = (
            "age > 2h: a canceled-without-fill order is no longer listed; a fill "
            "is still ruled out by the history and archive scans, and a "
            "canceled-without-fill order moves no funds"
        )
    if age > _EVIDENCE_HORIZON_SECONDS:
        # Past the archive retention a real fill would no longer be listed in
        # any queryable view, so this clean scan proves nothing. Absence of
        # evidence is not evidence of absence: the intent stays UNKNOWN and
        # needs an operator decision -- it is never auto-closed.
        return False, (
            f"intent age {age / 86400:.1f}d exceeds the "
            f"{_EVIDENCE_HORIZON_SECONDS / 86400:.0f}-day evidence horizon; "
            "beyond the exchange's archive window a fill would no longer be "
            "listed anywhere, so this scan cannot prove the order never "
            "existed. The intent stays UNKNOWN for a human decision."
        )
    return True, (
        f"no match in the order lookup, open orders, a "
        f"{findings['history_rows']}-row 7-day history or a "
        f"{findings['archive_rows']}-row 3-month archive; intent age "
        f"{age / 3600:.2f}h; {window}"
    )
