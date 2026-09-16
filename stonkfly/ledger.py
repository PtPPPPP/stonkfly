"""Durable money accounting and order intent. All values use Decimal strings."""

import contextlib
import json
import sqlite3
import uuid
from pathlib import Path

from .config import D


def _default_identity(mode):
    base = {"account": None, "quote_ccy": "USDC"}
    if mode == "okx-demo":
        return {**base, "exchange": "okx", "environment": "okx-demo"}
    if mode == "live":
        return {**base, "exchange": "coinbase", "environment": "live"}
    return {**base, "exchange": "paper", "environment": "paper"}


class Ledger:
    def __init__(self, path, settings, mode, identity=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY,status TEXT NOT NULL,created REAL NOT NULL,plan TEXT NOT NULL,exchange_id TEXT,settlement TEXT)"
        )
        self._identity = dict(identity) if identity is not None else _default_identity(mode)
        if self.get("settings") is None:
            with self.transaction():
                for k, v in {
                    "settings": settings.signature(),
                    "mode": mode,
                    "identity": self._identity,
                    "cash": settings.capital,
                    "initial_cash": settings.capital,
                    "positions": {},
                    "anchor": settings.capital,
                    "tick": 0,
                    "checkpoint": None,
                    "halted": None,
                    "last_attempt": 0,
                }.items():
                    self.put(k, v)
        elif self.get("settings") != settings.signature() or self.get("mode") != mode:
            raise RuntimeError(
                "Run settings/mode mismatch; use a separate paper run directory"
            )
        else:
            stored = self.get("identity")
            if stored is None:
                # Legacy ledger initialized before identity was recorded: its
                # exchange/environment/account context is unknown, so it must
                # not be silently adopted or re-bound to the current account.
                raise RuntimeError(
                    "Ledger lacks recorded identity; use a separate run directory"
                )
            if (
                stored.get("exchange") != self._identity["exchange"]
                or stored.get("environment") != self._identity["environment"]
                or stored.get("quote_ccy") != self._identity["quote_ccy"]
            ):
                raise RuntimeError(
                    "Ledger exchange/environment/quote mismatch; use a separate run directory"
                )

    def bind_account(self, account):
        ident = dict(self.get("identity") or self._identity)
        current = ident.get("account")
        if current is None:
            ident["account"] = account
            self.put("identity", ident)
        elif current != account:
            raise RuntimeError(
                "Account identity mismatch; use a separate run directory"
            )

    def check_account(self, account):
        ident = dict(self.get("identity") or self._identity)
        current = ident.get("account")
        if current is not None and current != account:
            raise RuntimeError("Account identity mismatch")

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            (key, json.dumps(value, allow_nan=False)),
        )

    @property
    def cash(self):
        return D(self.get("cash"))

    @property
    def positions(self):
        return {k: D(v) for k, v in self.get("positions").items()}

    def equity(self, quotes):
        return self.cash + sum(
            (v * quotes[p].bid for p, v in self.positions.items()), D(0)
        )

    def halt(self, reason):
        self.put("halted", reason)

    def reserve(self, plan, now):
        # OKX clOrdId allows only case-sensitive alphanumerics up to 32 chars
        # (no hyphens). Coinbase accepts the same 32-char hex form.
        cid = uuid.uuid4().hex
        plan = {**plan, "client_order_id": cid}
        with self.transaction():
            if self.pending():
                raise RuntimeError("Unreconciled order exists")
            self.db.execute(
                "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
                (cid, "PREPARED", now, json.dumps(plan)),
            )
            self.put("last_attempt", now)
        return plan

    def mark(self, cid, status, exchange_id=None):
        self.db.execute(
            "UPDATE orders SET status=?,exchange_id=COALESCE(?,exchange_id) WHERE id=?",
            (status, exchange_id, cid),
        )

    def pending(self):
        rows = self.db.execute(
            "SELECT id,status,created,plan,exchange_id FROM orders WHERE status NOT IN ('SETTLED','REJECTED') ORDER BY created"
        ).fetchall()
        return [
            {
                "id": r[0],
                "status": r[1],
                "created": r[2],
                "plan": json.loads(r[3]),
                "exchange_id": r[4],
            }
            for r in rows
        ]

    def attempts_today(self, now):
        return self.db.execute(
            "SELECT COUNT(*) FROM orders WHERE created>=?", (now - now % 86400,)
        ).fetchone()[0]

    def settle(self, cid, base, quote, fee, fee_ccy=None):
        base, quote, fee = map(D, (base, quote, fee))
        if base < 0 or quote < 0:
            raise ValueError("Negative settlement")
        if fee_ccy not in (None, "quote", "base"):
            raise ValueError("Unsupported fee currency")
        if (base == 0 and (quote or fee)) or (base > 0 and quote == 0):
            raise ValueError("Inconsistent fill quantities")
        with self.transaction():
            row = self.db.execute(
                "SELECT status,plan,settlement FROM orders WHERE id=?", (cid,)
            ).fetchone()
            if not row:
                raise RuntimeError("Unknown order")
            payload = {
                "base": str(base),
                "quote": str(quote),
                "fee": str(fee),
                "fee_ccy": fee_ccy or "quote",
            }
            if row[0] == "SETTLED":
                previous = dict(json.loads(row[2]))
                previous.setdefault("fee_ccy", "quote")
                if previous != payload:
                    raise RuntimeError("Settlement changed after finalization")
                return
            if row[0] == "REJECTED":
                raise RuntimeError("Cannot settle a rejected intent")
            p = json.loads(row[1])
            positions = self.positions
            held = positions.get(p["product"], D(0))
            cash = self.cash
            base_fee = fee if fee_ccy == "base" else D(0)
            quote_fee = fee if fee_ccy != "base" else D(0)
            if base > D(p["base_size"]):
                raise RuntimeError("Fill exceeds requested quantity")
            if p["side"] == "BUY":
                if quote > D(p["limit_price"]) * base + D(".00000001"):
                    raise RuntimeError("Buy fill exceeded limit price")
                cash -= quote + quote_fee
                positions[p["product"]] = held + base - base_fee
            else:
                if quote + D(".00000001") < D(p["limit_price"]) * base:
                    raise RuntimeError("Sell fill below limit price")
                cash += quote - quote_fee
                positions[p["product"]] = held - base - base_fee
            if cash < 0 or positions[p["product"]] < 0:
                raise RuntimeError("Fill exceeds reserved account funds")
            self.put("cash", str(cash))
            self.put("positions", {k: str(v) for k, v in positions.items()})
            self.db.execute(
                "UPDATE orders SET status='SETTLED',settlement=? WHERE id=?",
                (json.dumps(payload), cid),
            )
            # Fee ceiling is expressed in quote currency. A base-currency fee is
            # converted at the actual fill ratio; rebates (negative) never trip it.
            price = quote / base if base else D(0)
            cost_quote = (quote_fee if quote_fee > 0 else D(0)) + (
                base_fee if base_fee > 0 else D(0)
            ) * price
            if cost_quote > D(p["fee_ceiling"]) + D(".00000001"):
                self.halt(
                    "Actual fee exceeded preview ceiling; fill recorded, further orders stopped"
                )

    def commit_tick(self, anchor, checkpoint, observation=None):
        with self.transaction():
            self.put("anchor", str(anchor))
            self.put("checkpoint", checkpoint)
            self.put("tick", self.get("tick") + 1)
            if observation is not None:
                self.put("observation", observation)

    def close(self):
        self.db.close()
