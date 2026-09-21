"""Build one authoritative run ledger from the existing run directories.

This is the migration step that ends the split accounting. It is deliberately
conservative and never touches a source directory:

  * it writes to a *new* directory (default ``runs/okx-authoritative-staging``)
    and refuses if that directory already holds a ledger, so it can never
    overwrite or reset an existing budget;
  * the unified baseline is the *earliest* recorded pre-trade snapshot, so base
    an earlier directory bought is never reclassified as a gift;
  * every settled exchange order is carried over exactly once, deduplicated by
    exchange order identity;
  * consumed attempts are carried over in full and can never be refunded, and no
    budget or attempt limit is regenerated;
  * adjudication history, retired acknowledgements and protocol migrations are
    preserved as history;
  * the reward anchor is re-based at the migration mark with a fresh public
    quote, and the discontinuity is recorded rather than hidden;
  * the resulting ledger is written with ``migration.state = "staged"``, and a
    staged ledger refuses to submit any order until it is explicitly promoted.

    .venv\\Scripts\\python.exe tools\\okx_ledger_migrate.py
    .venv\\Scripts\\python.exe tools\\okx_ledger_migrate.py --confirm-promotion

Promotion is the user's decision: review the report first.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stonkfly import audit  # noqa: E402
from stonkfly.config import D, Settings  # noqa: E402
from stonkfly import locking  # noqa: E402
from stonkfly.ledger import Ledger  # noqa: E402
from stonkfly.okx_broker import OKXBroker  # noqa: E402
from stonkfly.okx_client import demo_client_from_env  # noqa: E402

EXCHANGE_ENV = "okx-demo"
STAGING = _ROOT / "runs" / "okx-authoritative-staging"


# --- checkpoint handling ---------------------------------------------------
# The checkpoint is the neural state itself. It is therefore never regenerated,
# merged, re-derived or reconstructed: the file recorded by the migration is
# copied byte for byte from the directory it was chosen from, with the hash
# checked on both sides of the copy.
CHECKPOINT_NAME = re.compile(r"^brain-\d+\.npz$")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checkpoint_path(target_dir, name):
    """Resolve a ledger-recorded checkpoint name inside the run directory.

    Refuses anything that is not a plain ``brain-N.npz`` file name, so a recorded
    path can never escape the run directory or address something else.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("checkpoint file name is missing")
    if not CHECKPOINT_NAME.match(name) or Path(name).name != name:
        raise ValueError(f"checkpoint file name {name!r} is not a plain brain-N.npz")
    resolved = (Path(target_dir) / name).resolve()
    if resolved.parent != Path(target_dir).resolve():
        raise ValueError("checkpoint path escapes the run directory")
    return resolved


def checkpoint_restorable(path, controller_factory=None):
    """True when the current FlyController can actually restore this checkpoint.

    A matching hash proves the bytes are unchanged; it does not prove the state is
    compatible with the current graph, rule and configuration, which
    ``Brain.restore`` is what validates. So the restore is attempted rather than
    assumed."""
    if controller_factory is None:
        from stonkfly.neural.controller import FlyController

        controller_factory = lambda: FlyController(  # noqa: E731
            Settings(products=("BTC-USDT",))
        )
    try:
        controller_factory().restore(path)
    except Exception:  # noqa: BLE001 - any failure means "not restorable"
        return False
    return True


def copy_checkpoint(source_view, target_dir, checkpoint, controller_factory=None):
    """Copy the chosen checkpoint into ``target_dir``, verified end to end.

    Returns an audit record. Never overwrites a target file whose content
    differs, never regenerates neural state, and refuses if the source no longer
    matches the hash the migration recorded.
    """
    if not isinstance(checkpoint, dict):
        raise ValueError("the migration recorded no checkpoint to copy")
    name = checkpoint.get("file")
    recorded = checkpoint.get("sha256")
    if not recorded:
        raise ValueError("the migration recorded no checkpoint hash")
    target = checkpoint_path(target_dir, name)
    source_dir = Path(source_view["path"]).parent
    source = checkpoint_path(source_dir, name)
    if not source.exists():
        raise ValueError(f"the chosen source directory has no {name}")
    actual_source = sha256_file(source)
    if actual_source != recorded:
        raise ValueError(
            f"the source {name} no longer matches the recorded hash "
            f"({actual_source[:12]} != {recorded[:12]})"
        )
    if target.exists():
        if sha256_file(target) == recorded:
            return {"file": name, "sha256": recorded, "result": "already_present",
                    "source": str(source)}
        raise ValueError(
            f"refusing to overwrite an existing {name} whose content differs"
        )
    # Write beside the target and rename, so a crash cannot leave a half-written
    # checkpoint where a run would pick it up.
    temporary = target.with_suffix(".partial")
    temporary.write_bytes(source.read_bytes())
    os.replace(temporary, target)
    if sha256_file(target) != recorded:
        raise ValueError("the copied checkpoint does not match the recorded hash")
    return {
        "file": name,
        "sha256": recorded,
        "result": "copied",
        "source": str(source),
        "bytes": target.stat().st_size,
    }


def load_sources(root):
    views = [
        v
        for v in audit.scan_runs(root)
        if audit.identity_of(v).get("environment") == EXCHANGE_ENV
        and audit.account_of(v)
        and v["meta"].get("baseline")
        and not audit.is_derived(v)
    ]
    timeline = []
    for v in views:
        stamps = [o["created"] for o in v["orders"]]
        stamps += [e.get("at") for e in v["meta"].get("resolved_orders") or []]
        stamps = [s for s in stamps if s]
        at = v["meta"].get("created_at") or (min(stamps) if stamps else None)
        timeline.append({"view": v, "at": at})
    # A directory with no timeline evidence cannot be placed; it must never be
    # treated as the earliest one.
    return [t for t in sorted(timeline, key=lambda t: (t["at"] is None, t["at"] or 0))]


def public_mark(client, product):
    """A fresh public price, used only to re-base the reward anchor."""
    ticker = client.ticker(product)
    ask = ticker.get("askPx") if ticker else None
    return D(ask) if ask else None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="okx_ledger_migrate")
    parser.add_argument("--target", type=Path, default=STAGING)
    parser.add_argument(
        "--confirm-promotion",
        action="store_true",
        help="Promote an already-verified staged ledger so it may submit orders.",
    )
    parser.add_argument(
        "--repair-checkpoint",
        action="store_true",
        help="Copy a missing checkpoint into an existing staged directory (file + audit only).",
    )
    parser.add_argument(
        "--check-promotion",
        action="store_true",
        help="Report whether promotion would be allowed, without changing anything.",
    )
    args = parser.parse_args(argv)
    load_dotenv(dotenv_path=_ROOT / ".env", override=False)

    if args.confirm_promotion and args.check_promotion:
        parser.error("choose one of --confirm-promotion / --check-promotion")
    if args.confirm_promotion:
        return promote(args.target)
    if args.check_promotion:
        return check_promotion(args.target)
    if args.repair_checkpoint:
        return repair_checkpoint(args.target)

    ordered = load_sources(_ROOT / "runs")
    if not ordered:
        print("no exchange-backed run directories with a baseline were found")
        return 1
    if (args.target / "ledger.sqlite").exists():
        print(f"refusing: {args.target} already holds a ledger; nothing was changed")
        return 1

    views = [t["view"] for t in ordered]
    # The earliest directory that can actually be placed on the timeline is the
    # cross-check reference. Anything later may already have absorbed its buys.
    placed = [t for t in ordered if t["at"]]
    if not placed:
        print("refusing: no source directory has any timeline evidence")
        return 1
    client = demo_client_from_env(timeout=20)
    reference_view = placed[0]["view"]
    reference_baseline = {
        ccy: D(v) for ccy, v in (reference_view["meta"]["baseline"] or {}).items()
    }

    # The unified protocol must be the one the sources actually ran.
    settings = Settings(products=("BTC-USDT",))
    signatures = {v["meta"].get("settings") for v in views}
    if signatures != {settings.signature()}:
        print("refusing: the sources did not run the protocol these settings produce")
        print(f"  source signatures: {sorted(str(s)[:12] for s in signatures)}")
        print(f"  candidate signature: {settings.signature()[:12]}  products={settings.products}")
        return 1

    union, conflicts = audit.union_orders(views)
    if conflicts:
        print(f"refusing: {len(conflicts)} conflicting settlements for the same order")
        return 1

    exchange_cash = {
        d["ccy"]: D(d["cashBal"])
        for d in client.balance()[0].get("details", [])
        if d.get("ccy")
    }
    union_totals = audit.totals([u["order"] for u in union])
    quote_ccy = reference_view["meta"]["identity"]["quote_ccy"]

    # The gift is *derived*, never adopted from a snapshot: every directory other
    # than the earliest absorbed the buys of the ones before it, so taking one of
    # their baselines would silently reclassify bot inventory as a gift. The
    # earliest baseline then acts as a real cross-check on completeness.
    union_orders = [u["order"] for u in union]
    gift, positions = audit.derive_gift(exchange_cash, union_orders, quote_ccy)
    cross_check = audit.cross_check_gift(gift, reference_baseline, quote_ccy)
    if not all(row["match"] for row in cross_check):
        print("refusing: the derived gift disagrees with the earliest baseline,")
        print("which means bot activity is missing from the union:")
        for row in cross_check:
            if not row["match"]:
                print(f"  {row['ccy']}: derived={row['derived_gift']} "
                      f"recorded={row['earliest_recorded_baseline']} delta={row['delta']}")
        return 1

    # The unified protocol must be the one the sources actually ran.
    settings = Settings(products=("BTC-USDT",))
    signatures = {v["meta"].get("settings") for v in views}
    if signatures != {settings.signature()}:
        print("refusing: the sources did not run the protocol these settings produce")
        print(f"  source signatures: {sorted(str(s)[:12] for s in signatures)}")
        print(f"  candidate signature: {settings.signature()[:12]}  products={settings.products}")
        return 1

    union, conflicts = audit.union_orders(views)
    if conflicts:
        print(f"refusing: {len(conflicts)} conflicting settlements for the same order")
        return 1

    exchange_cash = {
        d["ccy"]: D(d["cashBal"])
        for d in client.balance()[0].get("details", [])
        if d.get("ccy")
    }
    union_totals = audit.totals([u["order"] for u in union])
    quote_ccy = reference_view["meta"]["identity"]["quote_ccy"]

    # The gift is *derived*, never adopted from a snapshot: every directory other
    # than the earliest absorbed the buys of the ones before it, so taking one of
    # their baselines would silently reclassify bot inventory as a gift. Deriving
    # it as "what the account holds now, minus everything the bot is known to
    # have done" keeps that impossible, and the earliest baseline is then a real
    # cross-check on the completeness of the union.
    gift = dict(exchange_cash)
    gift[quote_ccy] = exchange_cash.get(quote_ccy, D(0)) + union_totals["quote"] + union_totals["fee_quote"]
    for u in union:
        product = u["order"]["plan"].get("product") or ""
        if "-" not in product:
            continue
        base_ccy = product.split("-")[0]
        s = u["order"]["settlement"]
        gained = D(s["base"]) - (D(s["fee"]) if s.get("fee_ccy") == "base" else D(0))
        if u["order"]["plan"].get("side") == "SELL":
            gained = -gained
        gift[base_ccy] = gift.get(base_ccy, D(0)) - gained

    cross_check = []
    for ccy, recorded in sorted(reference_baseline.items()):
        derived = gift.get(ccy, D(0))
        tol = D("0.0001") if ccy == quote_ccy else D("0.00000001")
        cross_check.append(
            {
                "ccy": ccy,
                "derived_gift": str(derived),
                "earliest_recorded_baseline": str(recorded),
                "delta": str(derived - recorded),
                "match": abs(derived - recorded) <= tol,
            }
        )
    if not all(row["match"] for row in cross_check):
        print("refusing: the derived gift disagrees with the earliest baseline,")
        print("which means bot activity is missing from the union:")
        for row in cross_check:
            if not row["match"]:
                print(f"  {row['ccy']}: derived={row['derived_gift']} "
                      f"recorded={row['earliest_recorded_baseline']} delta={row['delta']}")
        return 1

    # Carry every order, deduplicated, and every adjudication and acknowledgement.
    carried = {}
    for view in views:
        for order in view["orders"]:
            key = order["exchange_id"] or order["id"]
            carried.setdefault(key, {"order": order, "sources": []})["sources"].append(
                view["path"]
            )
    adjudications = [a for v in views for a in audit.adjudications(v)]
    retired_acks = [v["meta"].get("algo_coverage_gap_ack") for v in views]
    retired_acks = [a for a in retired_acks if a]
    migrations = [m for v in views for m in (v["meta"].get("protocol_migrations") or [])]
    attempts = sum(audit.attempts_used(v) for v in views)

    settled_totals = union_totals
    budget = D(reference_view["meta"]["budget"] or settings.capital)
    cash = budget - settled_totals["quote"] - settled_totals["fee_quote"]

    # Checkpoint choice: the newest directory on the verifiable timeline wins.
    # Checkpoints are never averaged or spliced; the alternates are recorded so
    # the choice can be reviewed.
    candidates = [
        {
            "path": t["view"]["path"],
            "at": t["at"],
            "checkpoint": t["view"]["meta"].get("checkpoint"),
            "tick": t["view"]["meta"].get("tick"),
        }
        for t in ordered
        if t["view"]["meta"].get("checkpoint")
    ]
    chosen = candidates[-1] if candidates else None

    account = audit.account_of(reference_view)
    identity = {
        "exchange": "okx",
        "environment": EXCHANGE_ENV,
        "account": account,
        "quote_ccy": quote_ccy,
    }
    ledger = Ledger(args.target / "ledger.sqlite", settings, "okx-demo", identity=identity)
    anchor = None
    try:
        with ledger.transaction():
            ledger.put("baseline", {c: str(v) for c, v in gift.items()})
            ledger.put("budget", str(budget))
            ledger.put("cash", str(cash))
            ledger.put("positions", {k: str(v) for k, v in positions.items()})
            ledger.put("initial_cash", settings.capital)
            # No attempt limit is invented here: the limit is a policy decision
            # for the run that follows, and the consumed count is carried in full.
            ledger.put("attempt_limit", None)
            ledger.put("order_attempts", attempts)
            ledger.put("tick", (chosen or {}).get("tick") or 0)
            ledger.put("checkpoint", (chosen or {}).get("checkpoint"))
            ledger.put("resolved_orders", adjudications or None)
            ledger.put("algo_coverage_gap_ack", retired_acks or None)
            ledger.put("protocol_migrations", migrations or None)
            ledger.put("demo_initialized", True)
            ledger.put("halted", None)
            for entry in carried.values():
                order = entry["order"]
                ledger.db.execute(
                    "INSERT INTO orders(id,status,created,plan,exchange_id,settlement)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        order["id"],
                        "SETTLED" if order["status"] == "SETTLED" else "REJECTED",
                        order["created"],
                        json.dumps(order["plan"]),
                        order["exchange_id"],
                        json.dumps(order["settlement"]) if order["settlement"] else None,
                    ),
                )
        # The ledger records a checkpoint; the directory must actually hold it,
        # copied and verified from the directory it was chosen from.
        checkpoint_record = None
        if chosen and chosen.get("checkpoint"):
            chosen_view = next(
                v for v in views if v["path"] == chosen["path"]
            )
            checkpoint_record = copy_checkpoint(
                chosen_view, args.target, chosen["checkpoint"]
            )

        mark = None
        if positions:
            mark = public_mark(client, next(iter(positions)))
        equity = cash + sum(
            (qty * (mark or D(0)) for qty in positions.values()), D(0)
        )
        with ledger.transaction():
            ledger.put("anchor", str(equity if mark else cash))
            ledger.put(
                "migration",
                {
                    "state": "staged",
                    "built_at": time.time(),
                    "sources": [t["view"]["path"] for t in ordered],
                    "gift_derived_from": "live account minus union of bot activity",
                    "gift_cross_checked_against": reference_view["path"],
                    "gift_cross_check": cross_check,
                    "carried_orders": len(carried),
                    "settled_orders": len(union),
                    "attempts_carried": attempts,
                    "checkpoint_candidates": candidates,
                    "checkpoint_chosen": chosen,
                    "checkpoint_source": (checkpoint_record or {}).get("source"),
                    "checkpoint_result": (checkpoint_record or {}).get("result"),
                    "anchor_rebased": bool(mark),
                    "anchor_mark": str(mark) if mark else None,
                    "adjudications_carried": len(adjudications),
                    "retired_acknowledgements": retired_acks,
                },
            )

        # Re-read the written ledger and verify conservation end to end.
        rows = audit.conservation(ledger_view(ledger), exchange_cash, budget)
    finally:
        ledger.close()

    report = {
        "target": str(args.target),
        "sources": [t["view"]["path"] for t in ordered],
        "gift_cross_checked_against": reference_view["path"],
        "gift_cross_check": cross_check,
        "gift": {c: str(v) for c, v in gift.items()},
        "budget": str(budget),
        "cash": str(cash),
        "positions": {k: str(v) for k, v in positions.items()},
        "attempts_carried": attempts,
        "carried_orders": len(carried),
        "settled_orders": len(union),
        "checkpoint_chosen": chosen,
        "checkpoint_candidates": candidates,
        "conservation": [
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()} for row in rows
        ],
        "conservation_ok": all(row["match"] for row in rows),
    }
    out_dir = _ROOT / "runs" / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"migration-{int(time.time())}.json"
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n")

    for row in rows:
        print(f"conservation {row['ccy']}: match={row['match']} delta={row['delta']}")
    # A target may legitimately live outside the project, so never assume it is
    # under the repository when rendering the summary.
    try:
        shown = args.target.resolve().relative_to(_ROOT).as_posix()
    except ValueError:
        shown = str(args.target)
    print(f"target: {shown} (migration.state = staged)")
    print(f"attempts_carried: {attempts}  settled_orders_carried: {len(union)}")
    print("report: " + out_file.relative_to(_ROOT).as_posix() + " (Git-ignored)")
    return 0 if report["conservation_ok"] else 1


def ledger_view(ledger):
    """A minimal view shaped like :func:`audit.read_ledger` for the new ledger."""
    return {
        "path": str(ledger.path),
        "meta": {k: ledger.get(k) for k in (
            "identity", "baseline", "budget", "cash", "positions",
        )},
        "orders": [],
    }


def _with_locked_staged_ledger(target, action, controller_factory=None):
    """Open a staged ledger under the run-directory lock and call ``action``.

    Shared by the check and the promotion so both run the identical
    precondition evaluation on an identically opened ledger.
    """
    path = target / "ledger.sqlite"
    if not path.exists():
        print(f"no ledger at {path}")
        return None
    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    settings = Settings(products=("BTC-USDT",))
    view = audit.read_ledger(path)
    if view["meta"].get("settings") != settings.signature():
        print("these settings did not produce that ledger's protocol")
        return None
    try:
        lock = locking.acquire(target / "worker.lock")
    except BlockingIOError:
        print("a worker owns this run directory right now")
        return None
    try:
        ledger = Ledger(path, settings, "okx-demo", identity=view["meta"]["identity"])
        try:
            broker = OKXBroker(settings, ledger, demo_client_from_env(timeout=25))
            return action(ledger, view, broker, settings, controller_factory)
        finally:
            ledger.close()
    finally:
        locking.release(lock)


def check_promotion(target):
    """Report whether promotion would be allowed. Changes nothing."""

    def action(ledger, view, broker, settings, controller_factory):
        migration = view["meta"].get("migration") or {}
        print(f"migration.state: {migration.get('state')!r}")
        problems = promotion_preconditions(
            view, broker, settings.capital, controller_factory
        )
        if problems:
            print("promotion would be refused:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print("promotion would be allowed (nothing was changed)")
        return 0

    result = _with_locked_staged_ledger(target, action)
    return 1 if result is None else result


def promotion_preconditions(view, broker, capital, controller_factory=None):
    """Everything that must hold right now before a ledger may be promoted.

    Returns a list of human-readable failures; empty means the preconditions hold.
    """
    problems = []
    migration = view["meta"].get("migration") or {}
    if migration.get("state") != "staged":
        problems.append(f"migration.state is {migration.get('state')!r}, not 'staged'")
        return problems

    # 1. The migration artifact must still be internally consistent: the counts it
    #    recorded must match the ledger it produced.
    rows = len(view["orders"])
    if migration.get("carried_orders") != rows:
        problems.append(
            f"carried_orders={migration.get('carried_orders')} but the ledger holds {rows}"
        )
    if migration.get("attempts_carried") != audit.attempts_used(view):
        problems.append(
            f"attempts_carried={migration.get('attempts_carried')} but the ledger "
            f"records {audit.attempts_used(view)}"
        )
    for row in migration.get("gift_cross_check") or []:
        if not row.get("match"):
            problems.append(f"gift cross-check failed for {row.get('ccy')}")
    for field in ("baseline", "budget", "cash", "positions"):
        if view["meta"].get(field) is None:
            problems.append(f"{field} is missing from the migrated ledger")
    if view["meta"].get("budget") is None or D(view["meta"]["budget"]) != D(capital):
        problems.append("budget does not match the configured capital")
    if [o for o in view["orders"] if o["status"] not in ("SETTLED", "REJECTED")]:
        problems.append("the ledger still holds an unresolved intent")

    # 2. The checkpoint the ledger points at must be present, legal, unchanged and
    #    actually restorable by the current controller -- a ledger whose neural
    #    state cannot be loaded is not a ledger a run can start from.
    target_dir = Path(view["path"]).parent
    cp = view["meta"].get("checkpoint")
    if not isinstance(cp, dict) or not cp.get("file") or not cp.get("sha256"):
        problems.append("checkpoint metadata is missing or incomplete")
    else:
        try:
            path = checkpoint_path(target_dir, cp["file"])
        except ValueError as e:
            problems.append(f"checkpoint path is not legal: {e}")
        else:
            if not path.exists():
                problems.append(
                    f"checkpoint file {cp['file']} is missing from the run directory"
                )
            elif sha256_file(path) != cp["sha256"]:
                problems.append(
                    f"checkpoint file {cp['file']} does not match the ledger hash"
                )
            elif not checkpoint_restorable(path, controller_factory):
                problems.append(
                    f"the current FlyController cannot restore {cp['file']}"
                )

    # 3. Current preconditions, checked live and read-only: identity, account
    #    mode, the risk set, untriggered algo coverage and balance
    #    reconciliation, exactly as a run would check them before its first tick.
    try:
        broker.verify_balances()
    except Exception as e:  # noqa: BLE001 - the category is enough here
        problems.append(f"live preconditions failed: {type(e).__name__}")
    return problems


def repair_checkpoint(target):
    """Fill a missing checkpoint in an existing staged directory, under the lock.

    Only the checkpoint file and an append-only audit entry are written. Cash,
    positions, budget, orders, attempt counters, the reward anchor, the checkpoint
    *metadata* and `migration.state` are all left exactly as they were: this
    completes a prepared migration, it does not redo it.
    """
    path = target / "ledger.sqlite"
    if not path.exists():
        print(f"no ledger at {path}")
        return 1
    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    settings = Settings(products=("BTC-USDT",))
    view = audit.read_ledger(path)
    if view["meta"].get("settings") != settings.signature():
        print("these settings did not produce that ledger's protocol")
        return 1
    migration = view["meta"].get("migration") or {}
    chosen = migration.get("checkpoint_chosen") or {}
    if not chosen.get("path"):
        print("refusing: the migration records no chosen checkpoint source")
        return 1
    cp = view["meta"].get("checkpoint")
    if not isinstance(cp, dict) or not cp.get("file"):
        print("refusing: the ledger records no checkpoint to restore")
        return 1
    try:
        lock = locking.acquire(target / "worker.lock")
    except BlockingIOError:
        print("refusing: a worker owns this run directory right now")
        return 1
    try:
        source_view = audit.read_ledger(Path(chosen["path"]))
        try:
            record = copy_checkpoint(source_view, target, cp)
        except ValueError as e:
            print(f"refusing: {e}")
            return 1
        resolved = checkpoint_path(target, cp["file"])
        if sha256_file(resolved) != cp["sha256"]:
            print("refusing: the restored file does not match the ledger hash")
            return 1
        ledger = Ledger(path, settings, "okx-demo", identity=view["meta"]["identity"])
        try:
            with ledger.transaction():
                repairs = ledger.get("checkpoint_repairs") or []
                repairs.append(
                    {
                        "at": time.time(),
                        "file": record["file"],
                        "sha256": record["sha256"],
                        "result": record["result"],
                        "source": record["source"],
                        "recorded_by": "tools/okx_ledger_migrate.py --repair-checkpoint",
                    }
                )
                ledger.put("checkpoint_repairs", repairs)
        finally:
            ledger.close()
        print(f"checkpoint {record['file']}: {record['result']} "
              f"(source {Path(record['source']).parent.name})")
        print("ledger money state, counters, anchor and migration state untouched")
        return 0
    finally:
        locking.release(lock)


def promote(target):
    """Promote a verified staged ledger, atomically, under the run-directory lock.

    Promotion is the point at which a migration copy becomes allowed to trade, so
    it re-checks the artifact's integrity and the live account preconditions
    rather than trusting that it was verified when it was built. The lock is taken
    first so no worker can be running against the directory, and the state flip
    happens inside one transaction that re-reads the state, so a concurrent change
    cannot be silently overwritten. Any failure leaves it staged.
    """

    def action(ledger, view, broker, settings, controller_factory):
        problems = promotion_preconditions(
            view, broker, settings.capital, controller_factory
        )
        if problems:
            print("refusing to promote; it stays staged:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        with ledger.transaction():
            migration = ledger.get("migration") or {}
            if migration.get("state") != "staged":
                raise RuntimeError(
                    "migration state changed between the checks and the promotion"
                )
            ledger.put(
                "migration",
                {
                    **migration,
                    "state": "promoted",
                    "promoted_at": time.time(),
                    "preconditions_verified": True,
                },
            )
        print("promoted: this ledger may now submit orders")
        print(f"  integrity: {len(view['orders'])} carried orders, "
              f"{audit.attempts_used(view)} attempts intact")
        print("  preconditions: identity, account mode, risk fields, untriggered "
              "algo coverage and balance reconciliation all passed")
        return 0

    result = _with_locked_staged_ledger(target, action)
    return 1 if result is None else result


if __name__ == "__main__":
    sys.exit(main())
