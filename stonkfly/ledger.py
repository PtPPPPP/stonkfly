"""Durable money accounting and order intent. All values use Decimal strings."""

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .config import D


class AttemptLimitReached(RuntimeError):
    """The persisted order-attempt budget for this run directory is exhausted."""


class MigrationNotPromoted(RuntimeError):
    """The ledger holds a prepared migration that the user has not confirmed.

    A migration copy exists to be reviewed, not run. It is built from historical
    orders and cannot place new ones until it is explicitly promoted.
    """


def _default_identity(mode, quote_ccy):
    base = {"account": None, "quote_ccy": quote_ccy}
    if mode == "okx-demo":
        return {**base, "exchange": "okx", "environment": "okx-demo"}
    if mode == "live":
        return {**base, "exchange": "coinbase", "environment": "live"}
    return {**base, "exchange": "paper", "environment": "paper"}


class Ledger:
    def __init__(self, path, settings, mode, identity=None, adopt_settings=False, staged_migration=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        try:
            self._initialize(settings, mode, identity, adopt_settings, staged_migration)
        except BaseException:
            self.db.close()
            raise

    def _initialize(self, settings, mode, identity, adopt_settings, staged_migration):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY,status TEXT NOT NULL,created REAL NOT NULL,plan TEXT NOT NULL,exchange_id TEXT,settlement TEXT)"
        )
        self._identity = dict(identity) if identity is not None else _default_identity(mode, settings.quote_ccy)
        if self.get("settings") is None:
            with self.transaction():
                for k, v in {
                    "settings": settings.signature(),
                    # Recorded so a later audit can place this directory on a
                    # timeline from the ledger itself rather than from mtimes.
                    "created_at": time.time(),
                    "mode": mode,
                    "identity": self._identity,
                    "cash": settings.capital,
                    "initial_cash": settings.capital,
                    "positions": {},
                    "anchor": settings.capital,
                    "budget": None,
                    "baseline": None,
                    "tick": 0,
                    "checkpoint": None,
                    "halted": None,
                    "last_attempt": 0,
                    "attempt_limit": None,
                    "order_attempts": None,
                    "algo_coverage_gap_ack": None,
                    "migration": {"state": "staged", "build_complete": False} if staged_migration else None,
                }.items():
                    self.put(k, v)
        else:
            if self.get("mode") != mode:
                raise RuntimeError("Run mode mismatch; use a separate run directory")
            stored = self.get("identity")
            if stored is None:
                raise RuntimeError(
                    "Ledger lacks recorded identity; use a separate run directory"
                )
            if any(
                stored.get(key) != self._identity[key]
                for key in ("exchange", "environment", "quote_ccy")
            ):
                raise RuntimeError(
                    "Ledger exchange/environment/quote mismatch; use a separate run directory"
                )
            if self._identity.get("account") is not None:
                self.check_account(self._identity["account"])

        if self.get("settings") != settings.signature():
            # Identity and mode were verified before any settings migration.
            if not adopt_settings:
                raise RuntimeError(
                    "Run settings/mismatch against this directory; review the "
                    "change, then use a separate run directory or pass "
                    "--migrate-protocol to adopt it in place"
                )
            with self.transaction():
                trail = self.get("settings_migrations") or []
                trail.append({
                    "at": time.time(),
                    "from": self.get("settings"),
                    "to": settings.signature(),
                    "note": "adopted via --migrate-protocol",
                })
                self.put("settings_migrations", trail)
                self.put("settings", settings.signature())
        # Seed the attempt counter exactly once per open. A directory written
        # before this counter existed has none, and its rows may already have
        # reached the exchange, so the starting count is the row count rather
        # than zero.
        if self.get("order_attempts") is None:
            rows = self.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            self.put("order_attempts", rows)

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

    def reject_prepared(self, cid):
        """Abandon only an intent that provably never crossed the send boundary."""
        self.db.execute(
            "UPDATE orders SET status='REJECTED' WHERE id=? AND status='PREPARED'",
            (cid,),
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

    def filled_today(self, now):
        """Settled orders with an actual fill in the current UTC day.

        The daily cap bounds real trading, so its units are fills: definite
        rejections and zero-fill FOK cancellations do not consume it (they
        still consume the lifetime attempt budget, which stays
        submission-based). A fill settlement carries base "0" only when
        nothing traded.
        """
        rows = self.db.execute(
            "SELECT settlement FROM orders WHERE created>=? AND status='SETTLED'",
            (now - now % 86400,),
        ).fetchall()
        return sum(1 for (s,) in rows if D(json.loads(s)["base"]) > 0)

    def filled_trade(self, cid):
        """The single semantic test for "this execution was a real trade".

        True only when the named intent reached SETTLED with an actual base
        fill (> 0). This is the layer every consumer must use instead of
        matching status strings: the paper broker reports its fills as
        "FILLED" while the OKX/Coinbase brokers report "SETTLED", and a
        zero-fill FOK cancellation is a legitimate SETTLED outcome that moved
        no money -- it must never grade as a trade (reward re-anchoring, daily
        caps, statistics all key off this).
        """
        row = self.db.execute(
            "SELECT status,settlement FROM orders WHERE id=?", (cid,)
        ).fetchone()
        if not row or row[0] != "SETTLED" or not row[1]:
            return False
        return D(json.loads(row[1])["base"]) > 0

    # -- order-attempt budget ---------------------------------------------
    # Separate from ``daily_orders``: that is a rolling rate limit, this is the
    # total number of submissions a single run directory may ever make. It is
    # enforced at the send boundary and persisted before the request can leave
    # the process, so a crash can only over-count (never under-count and then
    # resend). Definite rejections and lost responses both consume an attempt.
    def set_attempt_limit(self, limit, allow_change=False):
        """Bind this run directory to a maximum number of order attempts.

        ``limit`` 0 means unbounded (continuous operation). The used count is
        never reset, so restarting in the same directory resumes the same
        budget; changing the bound needs an explicit acknowledgement.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("Attempt limit must be a non-negative integer")
        with self.transaction():
            stored = self.get("attempt_limit")
            if stored is not None and stored != limit and not allow_change:
                raise RuntimeError(
                    f"This run directory is bound to a limit of {stored} order "
                    f"attempts. Pass --allow-attempt-limit-change to change it; "
                    f"the {self.attempts_used()} attempts already recorded are kept."
                )
            if stored != limit:
                # Every change of the submission budget -- including the first
                # binding and any move to unbounded (0) -- lands in an
                # append-only trail, so an operator can always see who widened
                # what and when.
                trail = self.get("attempt_limit_changes") or []
                trail.append(
                    {
                        "at": time.time(),
                        "from": stored,
                        "to": limit,
                        "attempts_used": self.attempts_used(),
                        "allow_change": bool(allow_change),
                    }
                )
                self.put("attempt_limit_changes", trail)
            self.put("attempt_limit", limit)

    def attempts_used(self):
        return self.get("order_attempts") or 0

    def attempts_remaining(self):
        """Attempts left, or ``None`` when the run directory is unbounded."""
        limit = self.get("attempt_limit")
        if not limit:
            return None
        return max(0, limit - self.attempts_used())

    def begin_attempt(self, cid):
        """Spend one attempt and mark the intent UNKNOWN, atomically.

        This is the send boundary: it is called immediately before the request
        that could place an order. The counter is committed first, so an
        interrupted run over-counts rather than repeating a submission.
        """
        with self.transaction():
            if (self.get("migration") or {}).get("state") == "staged":
                raise MigrationNotPromoted(
                    "This ledger holds an unconfirmed migration; it cannot submit "
                    "orders until the migration is promoted after review"
                )
            used = self.attempts_used()
            limit = self.get("attempt_limit")
            if limit and used >= limit:
                raise AttemptLimitReached(
                    f"Order attempt budget exhausted ({used}/{limit}); "
                    "no further submission is allowed"
                )
            changed = self.db.execute(
                "UPDATE orders SET status='UNKNOWN' WHERE id=? AND status='PREPARED'",
                (cid,),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Submission requires one PREPARED intent")
            self.put("order_attempts", used + 1)

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
            if p.get("side") not in ("BUY", "SELL"):
                raise ValueError("Invalid settlement side")
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

    def adjudicate_absent(self, cid, basis):
        """Close one specific UNKNOWN intent by human adjudication.

        Never called automatically. Normal start-up, ``reconcile`` and
        ``--resume-reviewed`` all leave an UNKNOWN intent untouched, because a
        failed query is not evidence that an order does not exist. This runs only
        when an operator names the exact intent, and even then it re-checks the
        row inside the transaction: the intent must still be UNKNOWN with no
        exchange order id, or nothing changes.

        The status change and the audit record are committed together, so a crash
        cannot close an intent without recording why, nor record a closure that
        did not happen. The record names its source and states plainly that this
        is an operator judgement, not an exchange confirmation.
        """
        with self.transaction():
            row = self.db.execute(
                "SELECT status,exchange_id,plan,created FROM orders WHERE id=?", (cid,)
            ).fetchone()
            if row is None:
                raise RuntimeError("Unknown order")
            if row[0] != "UNKNOWN" or row[1]:
                raise RuntimeError(
                    "Only an UNKNOWN intent with no exchange order id can be "
                    f"adjudicated; this one is {row[0]}"
                )
            plan = json.loads(row[2])
            entries = self.get("resolved_orders") or []
            record = {
                "id": cid,
                "reason": "adjudicated_absent",
                "source": "operator",
                "at": time.time(),
                "product": plan.get("product"),
                "order_type": plan.get("order_type"),
                "intent_created": row[3],
                "basis": basis,
                "confirmation": "human_adjudication_not_exchange_confirmation",
            }
            entries.append(record)
            self.put("resolved_orders", entries)
            self.db.execute("UPDATE orders SET status='REJECTED' WHERE id=?", (cid,))
        return record

    def migrate_protocol(self, signature, note=None):
        """Adopt a new source/protocol signature without touching money state.

        An append-only trail records the previous signature, the new one and the
        time. Cash, positions, baseline, budget, consumed attempts and the
        checkpoint are all left exactly as they were, so a migration can never
        hand back budget or submission attempts, and it skips no risk check --
        the full preflight still runs against the new source.
        """
        with self.transaction():
            previous = self.get("provenance_sha256")
            trail = self.get("protocol_migrations") or []
            trail.append(
                {
                    "from": previous,
                    "to": signature,
                    "at": time.time(),
                    "note": note,
                }
            )
            self.put("protocol_migrations", trail)
            self.put("provenance_sha256", signature)
        return previous

    def commit_tick(self, anchor, checkpoint, observation=None):
        """Commit one tick. ``anchor=None`` holds the reward anchor unchanged
        (trade-anchored reinforcement re-bases it at trades instead)."""
        with self.transaction():
            if anchor is not None:
                self.put("anchor", str(anchor))
            self.put("checkpoint", checkpoint)
            self.put("tick", self.get("tick") + 1)
            if observation is not None:
                self.put("observation", observation)

    def close(self):
        self.db.close()
