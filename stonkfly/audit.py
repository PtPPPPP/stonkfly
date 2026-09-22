"""Read-only inspection of run directories.

Deliberately independent of ``Ledger``: every connection here is opened
``mode=ro``, so an audit physically cannot alter what it is auditing, and a tool
does not need the ``Settings`` object a writable open would require. Nothing in
this module writes.

The aggregation helpers exist because several run directories traded against the
same exchange account over time. They deduplicate by exchange order identity and
compute the union of what the bot actually did, which is the only defensible
basis for unifying the accounting.
"""

import json
import sqlite3
from pathlib import Path

from .config import D

# A run directory is any directory holding this filename.
LEDGER_NAME = "ledger.sqlite"


def read_ledger(path):
    """Return a read-only view of one ledger: ``{"path", "meta", "orders"}``."""
    p = Path(path)
    path = p / LEDGER_NAME if p.is_dir() else p
    if not path.exists():
        raise FileNotFoundError(path)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("BEGIN")  # Metadata and orders must come from one snapshot.
        meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
        orders = [
            {
                "id": r[0],
                "status": r[1],
                "created": r[2],
                "plan": json.loads(r[3]),
                "exchange_id": r[4],
                "settlement": json.loads(r[5]) if r[5] else None,
            }
            for r in db.execute(
                "SELECT id,status,created,plan,exchange_id,settlement"
                " FROM orders ORDER BY created"
            )
        ]
    finally:
        db.close()
    return {"path": str(path), "meta": meta, "orders": orders}


def scan_runs(root="runs"):
    """Every run directory under ``root`` that holds a ledger, name-sorted."""
    base = Path(root)
    if not base.is_dir():
        return []
    return [
        read_ledger(d / LEDGER_NAME)
        for d in sorted(base.iterdir())
        if d.is_dir() and (d / LEDGER_NAME).exists()
    ]


def is_derived(view):
    """True for a ledger a migration built, i.e. not an independent source.

    A derived ledger copies orders that already exist elsewhere, so it must never
    be counted as separate activity or feed a later migration as a source.
    """
    return bool(view["meta"].get("migration"))


def identity_of(view):
    return dict(view["meta"].get("identity") or {})


def account_of(view):
    """The bound account id, or None. Never printed outside an ignored report."""
    return identity_of(view).get("account")


def settled(view):
    """Orders this ledger records as settled, newest last."""
    return [o for o in view["orders"] if o["status"] == "SETTLED"]


def pending(view):
    return [o for o in view["orders"] if o["status"] not in ("SETTLED", "REJECTED")]


def attempts_used(view):
    """Attempts this ledger has recorded.

    ``order_attempts`` is authoritative once present; a ledger written before the
    counter existed over-counts from its rows rather than under-counting.
    """
    stored = view["meta"].get("order_attempts")
    return stored if stored is not None else len(view["orders"])


def totals(orders):
    """Sum base acquired, quote spent and fees over settled orders.

    ``Ledger.settle`` stores a fee as a *positive cost* in the currency charged,
    and that is the convention here, so the account actually received
    ``base - fee_base`` and spent ``quote + fee_quote``. Getting this backwards
    silently doubles the fee in any conservation check.
    """
    base = D(0)
    quote = D(0)
    fee_base = D(0)
    fee_quote = D(0)
    for o in orders:
        s = o.get("settlement")
        if not s:
            continue
        base += D(s["base"])
        quote += D(s["quote"])
        fee = D(s["fee"])
        if s.get("fee_ccy") == "base":
            fee_base += fee
        else:
            fee_quote += fee
    return {
        "base": base,
        "quote": quote,
        "fee_base": fee_base,
        "fee_quote": fee_quote,
    }


def union_orders(views):
    """Deduplicate settled orders across ledgers by exchange order identity.

    A single exchange order must be counted once no matter how many directories
    recorded it. Identity is the exchange order id when known, else the client
    order id, and a disagreement between two directories about the same id is
    reported rather than resolved silently.
    """
    by_key = {}
    conflicts = []
    for view in views:
        for order in settled(view):
            key = order["exchange_id"] or order["id"]
            identity = identity_of(view)
            scoped_key = tuple(identity.get(k) for k in ("exchange", "environment", "account")) + (key,)
            existing = by_key.get(scoped_key)
            if existing is None:
                by_key[scoped_key] = {"key": key, "order": order, "sources": [view["path"]]}
                continue
            existing["sources"].append(view["path"])
            if (
                existing["order"]["settlement"] != order["settlement"]
                or any(existing["order"]["plan"].get(k) != order["plan"].get(k)
                       for k in ("product", "side", "base_size", "limit_price"))
            ):
                conflicts.append(
                    {
                        "key": key,
                        "a": existing["order"]["settlement"],
                        "b": order["settlement"],
                        "sources": existing["sources"],
                    }
                )
    return list(by_key.values()), conflicts


def unresolved(views):
    """Every intent that is neither settled nor definitively rejected."""
    out = []
    for view in views:
        for order in pending(view):
            out.append({"path": view["path"], "order": order})
    return out


def adjudications(view):
    """Resolutions recorded in a ledger, with a flag for the retired form.

    Early entries were written by a mechanism that turned a failed query into a
    REJECTED status without recording a basis. They are kept as history and
    flagged, because their evidence cannot be re-derived.
    """
    out = []
    for entry in view["meta"].get("resolved_orders") or []:
        out.append({**entry, "evidence_recorded": "basis" in entry})
    return out


def conservation(view, exchange_cash, budget):
    """Compare a ledger's expectation against actual exchange cash balances.

    expected = baseline + the bot's own net contribution, per currency, which is
    the same identity the broker reconciles against every tick.
    """
    meta = view["meta"]
    baseline = meta.get("baseline")
    if baseline is None or meta.get("budget") is None:
        return None
    quote = identity_of(view).get("quote_ccy")
    expected = {ccy: D(v) for ccy, v in baseline.items()}
    expected[quote] = expected.get(quote, D(0)) + (D(meta["cash"]) - D(budget))
    for product, amount in (meta.get("positions") or {}).items():
        base_ccy = product.split("-")[0]
        expected[base_ccy] = expected.get(base_ccy, D(0)) + D(amount)
    rows = []
    for ccy in sorted(set(expected) | set(exchange_cash)):
        want = expected.get(ccy, D(0))
        have = exchange_cash.get(ccy, D(0))
        tol = D("0.0001") if ccy == quote else D("0.00000001")
        rows.append(
            {
                "ccy": ccy,
                "expected": want,
                "actual": have,
                "delta": have - want,
                "match": abs(have - want) <= tol,
            }
        )
    return rows


def serialize(view):
    """A JSON-safe, complete view for an ignored local report."""
    meta = view["meta"]
    ident = identity_of(view)
    return {
        "path": view["path"],
        "identity": ident,
        "settings": meta.get("settings"),
        "mode": meta.get("mode"),
        "tick": meta.get("tick"),
        "cash": meta.get("cash"),
        "budget": meta.get("budget"),
        "positions": meta.get("positions"),
        "baseline": meta.get("baseline"),
        "halted": meta.get("halted"),
        "checkpoint": meta.get("checkpoint"),
        "provenance_sha256": meta.get("provenance_sha256"),
        "protocol_migrations": meta.get("protocol_migrations"),
        "algo_coverage_last": meta.get("algo_coverage_last"),
        "algo_coverage_gap_ack_retired": meta.get("algo_coverage_gap_ack"),
        "attempt_limit": meta.get("attempt_limit"),
        "order_attempts": attempts_used(view),
        "demo_initialized": meta.get("demo_initialized"),
        "adjudications": adjudications(view),
        "orders": [
            {
                "id": o["id"],
                "status": o["status"],
                "created": o["created"],
                "exchange_id": o["exchange_id"],
                "side": o["plan"].get("side"),
                "product": o["plan"].get("product"),
                "base_size": o["plan"].get("base_size"),
                "limit_price": o["plan"].get("limit_price"),
                "order_type": o["plan"].get("order_type"),
                "settlement": o["settlement"],
            }
            for o in view["orders"]
        ],
    }


def bot_contribution(orders, quote_ccy):
    """What the bot's own settled orders did to each currency.

    Returns ``(per_currency, per_product)``: the signed change the bot caused in
    every currency it touched, and the net base it still holds in each product.
    Fees follow the ledger convention (a positive cost in the currency charged),
    so a base-currency fee reduces the base gained and a quote-currency fee
    increases the quote spent.
    """
    per_currency = {}
    per_product = {}
    for order in orders:
        settlement = order.get("settlement")
        plan = order.get("plan") or {}
        product = plan.get("product") or ""
        if not isinstance(settlement, dict) or not settlement or product.count("-") != 1:
            raise ValueError("Incomplete settled order cannot be included in accounting")
        if plan.get("side") not in ("BUY", "SELL"):
            raise ValueError("Order side must be BUY or SELL")
        if settlement.get("fee_ccy", "quote") not in ("base", "quote"):
            raise ValueError("Unsupported fee currency")
        base_ccy, quote_ccy_of_product = product.split("-")[:2]
        if quote_ccy_of_product != quote_ccy:
            raise ValueError("Order quotes a different currency than the ledger")
        base = D(settlement["base"])
        quote = D(settlement["quote"])
        fee = D(settlement["fee"])
        if base < 0 or quote < 0 or (base == 0 and (quote or fee)) or (base > 0 and quote == 0):
            raise ValueError("Inconsistent settlement quantities")
        fee_in_base = settlement.get("fee_ccy") == "base"
        base_fee = fee if fee_in_base else D(0)
        quote_fee = D(0) if fee_in_base else fee
        # A fee always works against the trader, so it cannot be flipped along
        # with the side: a base fee reduces the base received on a buy and adds
        # to the base given up on a sell, and a quote fee does the mirror image.
        if plan.get("side") == "SELL":
            base_delta = -(base + base_fee)
            quote_delta = quote - quote_fee
        else:
            base_delta = base - base_fee
            quote_delta = -(quote + quote_fee)
        per_currency[base_ccy] = per_currency.get(base_ccy, D(0)) + base_delta
        per_currency[quote_ccy] = per_currency.get(quote_ccy, D(0)) + quote_delta
        per_product[product] = per_product.get(product, D(0)) + base_delta
    return per_currency, per_product


def derive_gift(exchange_cash, orders, quote_ccy):
    """The pre-bot balance of every currency, derived rather than adopted.

    ``gift = what the account holds now - what the bot is known to have done``.
    Taking a snapshot from one of the run directories instead would silently
    reclassify bot inventory as a gift, because every directory created after the
    first absorbed the buys of the ones before it.
    """
    per_currency, per_product = bot_contribution(orders, quote_ccy)
    gift = dict(exchange_cash)
    for ccy, delta in per_currency.items():
        gift[ccy] = gift.get(ccy, D(0)) - delta
    return gift, per_product


def cross_check_gift(gift, recorded_baseline, quote_ccy, tolerance=None):
    """Compare a derived gift against an independently recorded baseline.

    This is the assertion that the union of bot activity is complete: if a
    directory's earliest pre-trade snapshot disagrees with the derived gift, some
    activity is unaccounted for and the migration must not proceed.
    """
    rows = []
    for ccy in sorted(set(gift) | set(recorded_baseline)):
        want = D(gift.get(ccy, D(0)))
        have = D(recorded_baseline.get(ccy, D(0)))
        tol = tolerance or (D("0.0001") if ccy == quote_ccy else D("0.00000001"))
        rows.append(
            {
                "ccy": ccy,
                "derived_gift": str(want),
                "earliest_recorded_baseline": str(have),
                "delta": str(want - have),
                "match": abs(want - have) <= tol,
            }
        )
    return rows
