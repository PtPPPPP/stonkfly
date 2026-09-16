"""OKX demo-trading execution: cash spot, price-bounded FOK only.

Live OKX execution is explicitly not supported in this build. Demo requests
carry the ``x-simulated-trading: 1`` header and use demo credentials. Order
placement is a single submission with no retry; ambiguous outcomes stop the
worker for reconciliation instead of being resubmitted.

Intent is persisted (``PREPARED``) by the ledger before submission and flipped
to ``UNKNOWN`` before the request leaves the process, so a crash never leaves a
sent order untracked. Fees are handled per currency and sign (a rebate is never
abs'd into a charge).
"""

import os
import time

from .broker import UnresolvedOrder
from .config import D
from .okx_client import OKXClient, OKXTransportError

TERMINAL = {"filled", "canceled", "mmp_canceled"}


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

    @classmethod
    def from_env(cls, settings, ledger):
        if os.environ.get("OKX_LIVE") == "I_ACCEPT_REAL_TRADES":
            raise RuntimeError("OKX live execution is not supported in this build")
        api_key = os.environ.get("OKX_API_KEY")
        secret = os.environ.get("OKX_API_SECRET")
        passphrase = os.environ.get("OKX_API_PASSPHRASE")
        if not (api_key and secret and passphrase):
            raise RuntimeError(
                "Set OKX_API_KEY, OKX_API_SECRET and OKX_API_PASSPHRASE for OKX demo trading"
            )
        return cls(
            settings,
            ledger,
            OKXClient(api_key=api_key, secret=secret, passphrase=passphrase, demo=True),
        )

    # -- account state -----------------------------------------------------
    def _accounts(self):
        ccys = list(dict.fromkeys(["USDC"] + [p.split("-")[0] for p in self.s.products]))
        data = self.client.balance(ccys)
        if not data:
            raise RuntimeError("Balance data missing")
        result = {}
        for d in data[0].get("details") or []:
            ccy = d.get("ccy")
            avail = D(d.get("availBal") or "0")
            if D(d.get("frozenBal") or "0") != 0 or D(d.get("ordFrozen") or "0") != 0:
                raise RuntimeError("Reserved/frozen external balance")
            result[ccy] = avail
        return result

    def preflight(self):
        cfg = self.client.account_config()
        if cfg is None:
            raise RuntimeError("Account config unavailable")
        if cfg.get("acctLv") != "1":
            raise RuntimeError("Require spot (cash) account mode")
        perms = {p.strip() for p in (cfg.get("perm") or "").split(",") if p.strip()}
        if "trade" not in perms:
            raise RuntimeError("API key lacks trade permission")
        if "withdraw" in perms:
            raise RuntimeError("API key must not carry withdraw permission")
        uid = cfg.get("uid")
        if not uid:
            raise RuntimeError("Account identity missing")
        self.uid = uid
        # Bind (or verify) the account identity BEFORE reconcile: on mismatch
        # this raises without touching any order state, balances or demo
        # initialization. reconcile must only run once identity is confirmed.
        self.l.bind_account(uid)
        self.reconcile()
        balances = self._accounts()
        if not self.l.get("demo_initialized"):
            if (
                self.l.get("tick")
                or self.l.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            ):
                raise RuntimeError("Uninitialized demo ledger already has activity")
            if any(v for k, v in balances.items() if k != "USDC"):
                raise RuntimeError("Start the demo account with only USDC")
            cash = balances.get("USDC", D(0))
            if not 0 < cash <= D(self.s.capital):
                raise RuntimeError("Fund demo account with 0 < USDC <= configured cap")
            with self.l.transaction():
                for k in ["cash", "initial_cash", "anchor"]:
                    self.l.put(k, str(cash))
                self.l.put("demo_initialized", True)
        self.verify_balances()
        return {
            "mode": "okx-demo",
            "account": uid,
            "spot_cash_mode": True,
            "withdrawals_disabled": True,
        }

    def verify_balances(self):
        actual = self._accounts()
        expected = {"USDC": self.l.cash}
        for p, amount in self.l.positions.items():
            expected[p.split("-")[0]] = amount
        for ccy in set(actual) | set(expected):
            tolerance = D(".02") if ccy == "USDC" else D(".00000001")
            if abs(actual.get(ccy, D(0)) - expected.get(ccy, D(0))) > tolerance:
                raise RuntimeError(
                    "External balance change; stop and reconcile rather than treat deposits as profit"
                )
        for p in self.s.products:
            if self.client.orders_pending(p):
                raise RuntimeError("External/open order in demo account")

    # -- execution ---------------------------------------------------------
    def execute(self, plan, before_submit):
        cid = plan["client_order_id"]
        try:
            # Re-verify external balances and open orders at the send boundary.
            self.verify_balances()
            before_submit(plan)
        except Exception:
            self.l.mark(cid, "REJECTED")
            raise
        # Durable UNKNOWN transition precedes any request that can place an order.
        self.l.mark(cid, "UNKNOWN")
        payload = {
            "instId": plan["product"],
            "tdMode": "cash",
            "side": plan["side"].lower(),
            "ordType": "fok",
            "sz": plan["base_size"],
            "px": plan["limit_price"],
            "clOrdId": cid,
        }
        try:
            resp = self.client.place_order(payload)
        except OKXTransportError as e:
            raise UnresolvedOrder(
                "Submission outcome unknown; reconcile before any further trade"
            ) from e
        # Envelope validation. Only a well-formed, definitive rejection is
        # REJECTED; anything missing, malformed, or semantically ambiguous
        # keeps the intent UNKNOWN and stops. OKX documents that a non-zero
        # top-level code (e.g. "1" Operation failed, "50004" timeout, "50013"
        # system busy) does not confirm whether an order was placed, so the
        # only definite rejection is a successful request with a non-zero
        # per-order sCode.
        if not isinstance(resp, dict):
            raise UnresolvedOrder("Order response missing or malformed envelope")
        code = resp.get("code")
        if code is None or code == "":
            raise UnresolvedOrder("Order response lacked a result code")
        if code != "0":
            raise UnresolvedOrder("Order response was not a definite acceptance")
        data = resp.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise UnresolvedOrder("Order response lacked order data")
        item = data[0]
        scode = item.get("sCode")
        if scode is None or scode == "":
            raise UnresolvedOrder("Order data item lacked a result code")
        if scode != "0":
            # Definite per-order rejection (request succeeded, this order did not).
            self.l.mark(cid, "REJECTED")
            return {"status": "REJECTED", "mode": "okx-demo"}
        oid = item.get("ordId")
        if not oid:
            raise UnresolvedOrder("Accepted order lacked an unambiguous order id")
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
