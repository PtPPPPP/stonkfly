"""Audit, deduplication and ledger-unification tests.

Everything here is offline: ledgers are built in a temporary directory and the
exchange is represented by plain dictionaries. No network order is placed and no
existing run directory is read or modified.
"""

import json
import time

import pytest

from stonkfly import audit
from stonkfly.config import D, Settings
from stonkfly.ledger import Ledger, MigrationNotPromoted


def _ledger(tmp_path, name, identity=None):
    s = Settings(products=("BTC-USDT",))
    return Ledger(tmp_path / f"{name}.sqlite", s, "okx-demo", identity=identity)


def _order(cid, side, base, quote, fee, fee_ccy="base", product="BTC-USDT", ord_id=None):
    return {
        "id": cid,
        "status": "SETTLED",
        "created": time.time(),
        "plan": {"product": product, "side": side, "base_size": base},
        "exchange_id": ord_id or f"ord-{cid}",
        "settlement": {
            "base": base, "quote": quote, "fee": fee, "fee_ccy": fee_ccy,
        },
    }


# ---------------------------------------------------------------------------
# Fee sign convention: the audit must not double or drop a fee.
# ---------------------------------------------------------------------------

def test_base_fee_reduces_the_base_gained():
    orders = [_order("a", "BUY", "0.001", "75.0", "0.0000001")]
    totals = audit.totals(orders)
    assert totals["base"] == D("0.001")
    assert totals["fee_base"] == D("0.0000001")
    per_ccy, per_product = audit.bot_contribution(orders, "USDT")
    assert per_ccy["BTC"] == D("0.001") - D("0.0000001")
    assert per_ccy["USDT"] == D("-75.0")
    assert per_product["BTC-USDT"] == D("0.001") - D("0.0000001")


def test_quote_fee_increases_the_quote_spent():
    orders = [_order("a", "BUY", "0.001", "75.0", "0.075", fee_ccy="quote")]
    per_ccy, _ = audit.bot_contribution(orders, "USDT")
    assert per_ccy["BTC"] == D("0.001")
    assert per_ccy["USDT"] == -(D("75.0") + D("0.075"))


def test_a_sell_moves_both_currencies_the_other_way():
    orders = [_order("a", "SELL", "0.001", "75.0", "0.075", fee_ccy="quote")]
    per_ccy, per_product = audit.bot_contribution(orders, "USDT")
    assert per_ccy["BTC"] == -D("0.001")
    assert per_ccy["USDT"] == D("75.0") - D("0.075")
    assert per_product["BTC-USDT"] == -D("0.001")


def test_a_mixed_quote_product_is_refused():
    orders = [_order("a", "BUY", "0.001", "75.0", "0", product="BTC-USDC")]
    with pytest.raises(ValueError):
        audit.bot_contribution(orders, "USDT")


# ---------------------------------------------------------------------------
# Deduplication: one exchange order is counted once, in one ledger.
# ---------------------------------------------------------------------------

def test_the_same_order_in_two_ledgers_is_counted_once(tmp_path):
    a, b = _ledger(tmp_path, "a"), _ledger(tmp_path, "b")
    for ledger in (a, b):
        ledger.db.execute(
            "INSERT INTO orders(id,status,created,plan,exchange_id,settlement)"
            " VALUES (?,?,?,?,?,?)",
            ("same", "SETTLED", 1.0, json.dumps({"product": "BTC-USDT"}), "ord-1",
             json.dumps({"base": "0.001", "quote": "75", "fee": "0", "fee_ccy": "base"})),
        )
    views = [audit.read_ledger(a.path), audit.read_ledger(b.path)]
    union, conflicts = audit.union_orders(views)
    assert len(union) == 1
    assert len(union[0]["sources"]) == 2
    assert conflicts == []
    a.close()
    b.close()


def test_two_ledgers_disagreeing_about_one_order_is_reported(tmp_path):
    a, b = _ledger(tmp_path, "a"), _ledger(tmp_path, "b")
    for ledger, quote in ((a, "75"), (b, "80")):
        ledger.db.execute(
            "INSERT INTO orders(id,status,created,plan,exchange_id,settlement)"
            " VALUES (?,?,?,?,?,?)",
            ("same", "SETTLED", 1.0, json.dumps({"product": "BTC-USDT"}), "ord-1",
             json.dumps({"base": "0.001", "quote": quote, "fee": "0", "fee_ccy": "base"})),
        )
    views = [audit.read_ledger(a.path), audit.read_ledger(b.path)]
    _union, conflicts = audit.union_orders(views)
    assert len(conflicts) == 1
    a.close()
    b.close()


def test_attempts_are_summed_and_never_reduced_by_unification(tmp_path):
    a, b = _ledger(tmp_path, "a"), _ledger(tmp_path, "b")
    a.put("order_attempts", 3)
    b.put("order_attempts", 2)
    views = [audit.read_ledger(a.path), audit.read_ledger(b.path)]
    assert sum(audit.attempts_used(v) for v in views) == 5
    a.close()
    b.close()


def test_a_ledger_without_a_counter_over_counts_from_its_rows(tmp_path):
    a = _ledger(tmp_path, "a")
    for i in range(3):
        a.db.execute(
            "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
            (str(i), "SETTLED", 1.0, json.dumps({"product": "BTC-USDT"})),
        )
    a.db.execute("DELETE FROM meta WHERE key='order_attempts'")
    # Reopening seeds from the rows rather than silently reporting zero.
    reopened = _ledger(tmp_path, "a")
    assert audit.attempts_used(audit.read_ledger(reopened.path)) == 3
    assert audit.attempts_used(audit.read_ledger(reopened.path)) > 0
    reopened.close()


# ---------------------------------------------------------------------------
# Deriving the gift, and the cross-check that proves the union is complete.
# ---------------------------------------------------------------------------

def test_the_gift_is_derived_from_the_live_account():
    orders = [_order("a", "BUY", "0.001", "75.0", "0.0000001")]
    # The account net of that buy: 0.001 filled less the 0.0000001 base fee.
    exchange = {"USDT": D("925.0"), "BTC": D("1.0009999")}
    gift, positions = audit.derive_gift(exchange, orders, "USDT")
    # 1000 USDT and 1 BTC before the bot bought anything.
    assert gift["USDT"] == D("1000.0")
    assert gift["BTC"] == D("1.0")
    assert positions["BTC-USDT"] == D("0.001") - D("0.0000001")


def test_an_adopted_baseline_would_be_visibly_wrong():
    # A later directory's baseline already contains the bot's BTC; deriving the
    # gift from the account must not reproduce that, and the cross-check must
    # therefore disagree with it -- which is what stops the migration.
    orders = [_order("a", "BUY", "0.001", "75.0", "0")]
    exchange = {"USDT": D("925.0"), "BTC": D("1.001")}
    gift, _ = audit.derive_gift(exchange, orders, "USDT")
    absorbed = {"USDT": D("925.0"), "BTC": D("1.001")}
    rows = audit.cross_check_gift(gift, absorbed, "USDT")
    assert [r["match"] for r in rows] == [False, False]


def test_cross_check_agrees_with_a_genuine_pre_trade_baseline():
    orders = [_order("a", "BUY", "0.001", "75.0", "0.0000001")]
    exchange = {"USDT": D("925.0"), "BTC": D("1.001")}
    gift, _ = audit.derive_gift(exchange, orders, "USDT")
    rows = audit.cross_check_gift(gift, gift, "USDT")
    assert all(r["match"] for r in rows)


def test_gift_and_positions_conserve_the_account():
    orders = [
        _order("a", "BUY", "0.001", "75.0", "0.0000001"),
        _order("b", "SELL", "0.0004", "30.0", "0.00000004"),
        _order("c", "BUY", "0.0002", "15.0", "0.00000002"),
    ]
    exchange = {"USDT": D("909.99994"), "BTC": D("1.00079988"), "ETH": D("1")}
    gift, positions = audit.derive_gift(exchange, orders, "USDT")
    # Conservation: gift + what the bot holds/spent == the live account.
    assert gift["BTC"] + positions["BTC-USDT"] == exchange["BTC"]
    spent = D("100") - (D("100") - (D("75.0") + D("15.0") - D("30.0")))
    cash = D("100") - (D("75.0") + D("15.0") - D("30.0"))
    assert gift["USDT"] + (cash - D("100")) == exchange["USDT"]
    assert spent > D(0)
    # Untouched currencies are carried through unchanged.
    assert gift["ETH"] == exchange["ETH"] == D("1")


def test_conservation_detects_a_missing_order():
    # If an order is missing from the union the derived gift no longer matches
    # the earliest baseline, so the migration refuses instead of inventing a
    # gift that hides the gap.
    orders = [_order("a", "BUY", "0.001", "75.0", "0")]
    exchange = {"USDT": D("925.0"), "BTC": D("1.001")}
    gift, _ = audit.derive_gift(exchange, orders, "USDT")
    rows = audit.cross_check_gift(gift, {"USDT": D("1000.0"), "BTC": D("1.0")}, "USDT")
    assert all(r["match"] for r in rows)
    # Now pretend one buy never made it into the union.
    partial, _ = audit.derive_gift({"USDT": D("1000.0"), "BTC": D("1.0")}, orders, "USDT")
    assert not all(r["match"] for r in audit.cross_check_gift(partial, gift, "USDT"))


# ---------------------------------------------------------------------------
# A staged migration cannot trade; a promoted one still cannot until it passes.
# ---------------------------------------------------------------------------

def test_a_staged_ledger_refuses_to_spend_an_attempt(tmp_path):
    ledger = _ledger(tmp_path, "staged")
    ledger.set_attempt_limit(2)
    ledger.put("migration", {"state": "staged", "sources": ["old"]})
    ledger.db.execute(
        "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
        ("c" * 32, "PREPARED", time.time(), json.dumps({"product": "BTC-USDT"})),
    )
    with pytest.raises(MigrationNotPromoted):
        ledger.begin_attempt("c" * 32)
    # Nothing was spent and the intent is untouched.
    assert ledger.attempts_used() == 0
    assert ledger.pending()[0]["status"] == "PREPARED"
    ledger.close()


def test_a_promoted_ledger_may_spend_an_attempt_again(tmp_path):
    ledger = _ledger(tmp_path, "promoted")
    ledger.set_attempt_limit(2)
    ledger.put("migration", {"state": "promoted"})
    ledger.db.execute(
        "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
        ("c" * 32, "PREPARED", time.time(), json.dumps({"product": "BTC-USDT"})),
    )
    ledger.begin_attempt("c" * 32)
    assert ledger.attempts_used() == 1
    assert ledger.pending()[0]["status"] == "UNKNOWN"
    ledger.close()


# ---------------------------------------------------------------------------
# Protocol migration: a config change must not brick a directory, and must not
# hand back money state.
# ---------------------------------------------------------------------------

def test_protocol_migration_is_appended_and_keeps_money_state(tmp_path):
    ledger = _ledger(tmp_path, "run")
    ledger.set_attempt_limit(2)
    ledger.put("order_attempts", 2)
    ledger.put("cash", "21.5")
    ledger.put("positions", {"BTC-USDT": "0.001"})
    ledger.put("checkpoint", {"file": "brain-0.npz", "sha256": "x"})
    ledger.put("provenance_sha256", "old-signature")

    previous = ledger.migrate_protocol("new-signature", note="reviewed")

    assert previous == "old-signature"
    assert ledger.get("provenance_sha256") == "new-signature"
    trail = ledger.get("protocol_migrations")
    assert len(trail) == 1 and trail[0]["from"] == "old-signature"
    assert trail[0]["to"] == "new-signature"
    # Money state, budget and consumed attempts are untouched by a migration.
    assert ledger.get("cash") == "21.5"
    assert ledger.get("positions") == {"BTC-USDT": "0.001"}
    assert ledger.attempts_used() == 2
    assert ledger.attempts_remaining() == 0
    assert ledger.get("checkpoint") == {"file": "brain-0.npz", "sha256": "x"}
    ledger.close()


def test_protocol_migration_records_every_step(tmp_path):
    ledger = _ledger(tmp_path, "run")
    ledger.migrate_protocol("sig-b", note="first")
    ledger.migrate_protocol("sig-c", note="second")
    trail = ledger.get("protocol_migrations")
    assert [t["from"] for t in trail] == [None, "sig-b"]
    assert [t["to"] for t in trail] == ["sig-b", "sig-c"]
    ledger.close()
