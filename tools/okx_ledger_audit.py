"""Read-only audit of every run directory under runs/.

Answers, from local ledgers plus read-only exchange queries, the questions that
decide whether the accounting can be unified:

  * do the exchange-backed directories describe the same account, environment,
    quote currency and protocol?
  * what orders, attempts, halts and checkpoints does each one hold?
  * does each directory's recorded settlement match the exchange's own record?
  * where do their timelines sit relative to each other, and did any two overlap?
  * did a later directory's baseline absorb base that an earlier directory had
    bought (i.e. reclassify bot inventory as a gift)?
  * what in the account is original gift and what is historical bot activity?
  * are there duplicate bookings, missing fills, unknown intents or evidence of
    concurrent runs?

It writes nothing but its own Git-ignored report and never needs a writable
ledger. Which directory is "authoritative" is deliberately NOT decided here.

    .venv\\Scripts\\python.exe tools\\okx_ledger_audit.py
"""

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stonkfly import audit  # noqa: E402
from stonkfly.config import D  # noqa: E402
from stonkfly.okx_client import demo_client_from_env  # noqa: E402

EXCHANGE_ENV = "okx-demo"


def build_client():
    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    key = os.environ.get("OKX_API_KEY")
    secret = os.environ.get("OKX_API_SECRET")
    passphrase = os.environ.get("OKX_API_PASSPHRASE")
    if not (key and secret and passphrase):
        raise RuntimeError("Set OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE in .env")
    return demo_client_from_env(timeout=25)


def fetch_exchange(client):
    """The exchange's own record: every completed order it will still list."""
    history, history_complete = client.orders_history("SPOT")
    archive, archive_complete = client.orders_history_archive("SPOT")
    merged = {}
    for row in history + archive:
        merged.setdefault(row.get("ordId"), row)
    details = client.balance()[0].get("details") or []
    cash = {d["ccy"]: D(d["cashBal"]) for d in details if d.get("ccy")}
    avail = {d["ccy"]: D(d["availBal"]) for d in details if d.get("ccy")}
    return {
        "history_rows": len(history),
        "history_complete": history_complete,
        "archive_rows": len(archive),
        "archive_complete": archive_complete,
        "orders": merged,
        "cash": cash,
        "avail": avail,
    }


def timeline_of(view):
    """The earliest timestamp this ledger evidences, and where it came from."""
    stamps = [o["created"] for o in view["orders"]]
    stamps += [e.get("at") for e in view["meta"].get("resolved_orders") or []]
    stamps = [s for s in stamps if s]
    if view["meta"].get("created_at"):
        return view["meta"]["created_at"], "created_at"
    if stamps:
        return min(stamps), "earliest_recorded_activity"
    return None, "none"


def analyse(views, derived, others, exchange):
    """Build the audit findings. Pure computation over read-only snapshots."""
    findings = {"directories": [], "consistency": {}, "timeline": {}, "totals": {}}

    accounts = {audit.account_of(v) for v in views}
    identities = {
        (audit.identity_of(v).get("exchange"), audit.identity_of(v).get("environment"),
         audit.identity_of(v).get("quote_ccy"))
        for v in views
    }
    settings = {v["meta"].get("settings") for v in views}
    findings["consistency"] = {
        "exchange_backed_directories": [v["path"] for v in views],
        "derived_directories": [
            {
                "path": v["path"],
                "migration_state": (v["meta"].get("migration") or {}).get("state"),
                "sources": (v["meta"].get("migration") or {}).get("sources"),
                "carried_orders": (v["meta"].get("migration") or {}).get("carried_orders"),
                "attempts": audit.attempts_used(v),
            }
            for v in derived
        ],
        "distinct_accounts": len(accounts),
        "single_account": len(accounts) == 1,
        "distinct_exchange_environment_quote": sorted(map(str, identities)),
        "single_protocol": len(settings) == 1,
        "distinct_protocol_signatures": len(settings),
        "other_directories": [
            {
                "path": v["path"],
                "mode": v["meta"].get("mode"),
                "environment": audit.identity_of(v).get("environment"),
                "account_bound": bool(audit.account_of(v)),
                "orders": len(v["orders"]),
                "settled": len(audit.settled(v)),
                "note": "not an exchange-backed okx-demo directory",
            }
            for v in others
        ],
    }

    # Per-directory facts.
    timeline = []
    for view in views:
        meta = view["meta"]
        settled = audit.settled(view)
        t, source = timeline_of(view)
        timeline.append({"view": view, "at": t, "source": source, "settled": settled})
        findings["directories"].append(
            {
                "path": view["path"],
                "tick": meta.get("tick"),
                "cash": meta.get("cash"),
                "budget": meta.get("budget"),
                "positions": meta.get("positions"),
                "baseline": meta.get("baseline"),
                "attempts_used": audit.attempts_used(view),
                "attempt_limit": meta.get("attempt_limit"),
                "halted": meta.get("halted"),
                "checkpoint": (meta.get("checkpoint") or {}).get("file"),
                "provenance_sha256": meta.get("provenance_sha256"),
                "protocol_migrations": meta.get("protocol_migrations"),
                "orders": len(view["orders"]),
                "settled": len(settled),
                "rejected": len([o for o in view["orders"] if o["status"] == "REJECTED"]),
                "unresolved": len(audit.pending(view)),
                "adjudications": audit.adjudications(view),
                "retired_ack": meta.get("algo_coverage_gap_ack"),
                "timeline_at": t,
                "timeline_source": source,
                "totals": {k: str(v) for k, v in audit.totals(settled).items()},
                "first_order": min((o["created"] for o in view["orders"]), default=None),
                "last_order": max((o["created"] for o in view["orders"]), default=None),
            }
        )

    # Ordering and overlap: only directories that evidence a timeline can be
    # placed, so the rest are reported rather than guessed at.
    placed = [t for t in timeline if t["at"]]
    placed.sort(key=lambda t: t["at"])
    findings["timeline"] = {
        "order": [
            {"path": t["view"]["path"], "at": t["at"], "source": t["source"]}
            for t in placed
        ],
        "unplaced": [t["view"]["path"] for t in timeline if not t["at"]],
        "overlaps": [],
    }
    for a, b in zip(placed, placed[1:]):
        a_last = a["view"]["meta"].get("last_attempt") or 0
        a_max = max((o["created"] for o in a["view"]["orders"]), default=a["at"])
        b_min = min((o["created"] for o in b["view"]["orders"]), default=b["at"])
        if a_max and b_min and b_min < a_max:
            findings["timeline"]["overlaps"].append(
                {"earlier": a["view"]["path"], "later": b["view"]["path"],
                 "earlier_last_order": a_max, "later_first_order": b_min}
            )

    # Does a later baseline include an earlier directory's bot inventory? The
    # earliest baseline is the best available pre-trade snapshot; every later
    # directory should equal it plus the earlier directories' own contribution.
    if placed:
        earliest = placed[0]["view"]
        gift = {ccy: D(v) for ccy, v in (earliest["meta"].get("baseline") or {}).items()}
        findings["totals"]["earliest_baseline_path"] = earliest["path"]
        findings["totals"]["earliest_baseline"] = {
            ccy: str(v) for ccy, v in gift.items()
        }
        drift = []
        # Cumulative: a directory's baseline should equal the gift plus everything
        # every earlier directory had already bought or spent, so the accumulation
        # must start from the earliest directory's own settled orders.
        prior_settled = list(placed[0]["settled"])
        for entry in placed[1:]:
            view = entry["view"]
            base = {ccy: D(v) for ccy, v in (view["meta"].get("baseline") or {}).items()}
            prior = audit.totals(prior_settled)
            expected_base = gift.get("BTC", D(0)) + prior["base"] - prior["fee_base"]
            expected_quote = gift.get("USDT", D(0)) - prior["quote"] - prior["fee_quote"]
            drift.append(
                {
                    "path": view["path"],
                    "baseline_btc": str(base.get("BTC", D(0))),
                    "expected_from_gift_plus_prior_bot": str(expected_base),
                    "absorbed_prior_bot_base": abs(
                        base.get("BTC", D(0)) - expected_base
                    ) <= D("0.00000001"),
                    "baseline_usdt": str(base.get("USDT", D(0))),
                    "expected_usdt_from_gift_minus_spend": str(expected_quote),
                    "absorbed_prior_bot_spend": abs(
                        base.get("USDT", D(0)) - expected_quote
                    ) <= D("0.0001"),
                }
            )
            prior_settled.extend(entry["settled"])
        findings["totals"]["later_baseline_drift"] = drift

    # Union of real bot activity, deduplicated, and what it implies vs the gift.
    union, conflicts = audit.union_orders(views)
    merged = [u["order"] for u in union]
    totals = audit.totals(merged)
    findings["totals"].update(
        {
            "union_orders": len(union),
            "duplicate_bookings": [
                {"key": u["key"], "sources": u["sources"]}
                for u in union if len(u["sources"]) > 1
            ],
            "settlement_conflicts": conflicts,
            "union_base": str(totals["base"]),
            "union_quote": str(totals["quote"]),
            "union_fee_base": str(totals["fee_base"]),
            "union_fee_quote": str(totals["fee_quote"]),
            "total_attempts": sum(audit.attempts_used(v) for v in views),
            "unresolved_intents": [
                {"path": u["path"], "id": u["order"]["id"], "status": u["order"]["status"]}
                for u in audit.unresolved(views)
            ],
        }
    )

    # Exchange cross-check of every claimed order.
    claimed = {}
    for view in views:
        for order in audit.settled(view):
            key = order["exchange_id"] or order["id"]
            claimed.setdefault(key, []).append(view["path"])
    matched, unmatched_local, mismatched = [], [], []
    for u in union:
        ex = exchange["orders"].get(u["order"]["exchange_id"])
        if ex is None:
            unmatched_local.append({"key": u["key"], "sources": u["sources"]})
            continue
        s = u["order"]["settlement"]
        ok = (
            ex.get("state") == "filled"
            and D(ex.get("accFillSz") or "0") == D(s["base"])
            and abs(D(ex.get("avgPx") or "0") * D(s["base"]) - D(s["quote"])) <= D("0.01")
        )
        entry = {
            "key": u["key"],
            "exchange_state": ex.get("state"),
            "exchange_base": ex.get("accFillSz"),
            "local_base": s["base"],
            "exchange_px": ex.get("avgPx"),
            "local_quote": s["quote"],
            "matches": ok,
        }
        (matched if ok else mismatched).append(entry)
    # Filled exchange orders no ledger claims. Splitting them by time matters: a
    # trade taken while the bot was running would mean the account is shared,
    # whereas older ones are simply the account's own history, and they also mean
    # the earliest baseline is not a pristine gift.
    bot_times = [o["created"] for v in views for o in v["orders"]]
    first_bot, last_bot = (min(bot_times), max(bot_times)) if bot_times else (None, None)
    unclaimed = []
    for oid, o in exchange["orders"].items():
        if not oid or oid in claimed or o.get("state") != "filled":
            continue
        taken_at = D(o.get("cTime") or "0") / 1000 if o.get("cTime") else None
        during = bool(
            taken_at and first_bot and last_bot and first_bot <= float(taken_at) <= last_bot
        )
        unclaimed.append(
            {
                "ordId": oid,
                "clOrdId": o.get("clOrdId"),
                "side": o.get("side"),
                "accFillSz": o.get("accFillSz"),
                "cTime": o.get("cTime"),
                "during_bot_activity": during,
            }
        )
    unclaimed.sort(key=lambda e: e["cTime"] or "")
    findings["exchange_cross_check"] = {
        "claimed_orders": len(union),
        "matched": len(matched),
        "mismatched": mismatched,
        "not_found_at_exchange": unmatched_local,
        "unclaimed_filled_orders": unclaimed,
        "unclaimed_during_bot_activity": [u for u in unclaimed if u["during_bot_activity"]],
        "account_had_prior_activity": bool(unclaimed),
        "history_rows": exchange["history_rows"],
        "history_complete": exchange["history_complete"],
        "archive_rows": exchange["archive_rows"],
        "archive_complete": exchange["archive_complete"],
    }

    # Conservation: gift + union contribution must equal the actual account.
    gift_usdt = D(findings["totals"]["earliest_baseline"].get("USDT", "0"))
    gift_btc = D(findings["totals"]["earliest_baseline"].get("BTC", "0"))
    exp_usdt = gift_usdt - totals["quote"] - totals["fee_quote"]
    exp_btc = gift_btc + totals["base"] - totals["fee_base"]
    findings["totals"]["conservation"] = [
        {
            "ccy": "USDT",
            "gift": str(gift_usdt),
            "bot_contribution": str(-(totals["quote"] + totals["fee_quote"])),
            "expected": str(exp_usdt),
            "actual": str(exchange["cash"].get("USDT", D(0))),
            "delta": str(exchange["cash"].get("USDT", D(0)) - exp_usdt),
            "match": abs(exchange["cash"].get("USDT", D(0)) - exp_usdt) <= D("0.0001"),
        },
        {
            "ccy": "BTC",
            "gift": str(gift_btc),
            "bot_contribution": str(totals["base"] - totals["fee_base"]),
            "expected": str(exp_btc),
            "actual": str(exchange["cash"].get("BTC", D(0))),
            "delta": str(exchange["cash"].get("BTC", D(0)) - exp_btc),
            "match": abs(exchange["cash"].get("BTC", D(0)) - exp_btc) <= D("0.00000001"),
        },
    ]
    # Currencies never touched by the bot must still equal the gift.
    untouched = []
    for ccy, value in gift.items():
        if ccy in ("USDT", "BTC"):
            continue
        actual = exchange["cash"].get(ccy, D(0))
        untouched.append(
            {"ccy": ccy, "gift": str(value), "actual": str(actual),
             "match": value == actual, "delta": str(actual - value)}
        )
    findings["totals"]["untouched_gift"] = untouched
    return findings


def summarize(findings, out_file):
    """Sanitized terminal output: counts and conclusions, no identifiers."""
    c = findings["consistency"]
    t = findings["totals"]
    x = findings["exchange_cross_check"]
    print(f"exchange_backed_directories: {len(c['exchange_backed_directories'])} "
          f"(derived from a migration: {len(c['derived_directories'])})")
    print(f"single_account: {c['single_account']}  "
          f"single_protocol: {c['single_protocol']}  "
          f"distinct_protocol_signatures: {c['distinct_protocol_signatures']}")
    print(f"other_directories: {len(c['other_directories'])}")
    print("timeline_order:")
    for entry in findings["timeline"]["order"]:
        print(f"  {Path(entry['path']).parent.name} ({entry['source']})")
    if findings["timeline"]["unplaced"]:
        print(f"  unplaced (no local timeline evidence): "
              f"{[Path(p).parent.name for p in findings['timeline']['unplaced']]}")
    print(f"timeline_overlaps: {len(findings['timeline']['overlaps'])}")
    print(f"union_settled_orders: {t['union_orders']}  "
          f"total_attempts: {t['total_attempts']}  "
          f"unresolved_intents: {len(t['unresolved_intents'])}")
    print(f"duplicate_bookings: {len(t['duplicate_bookings'])}  "
          f"settlement_conflicts: {len(t['settlement_conflicts'])}")
    print(f"exchange_match: {x['matched']}/{x['claimed_orders']}  "
          f"mismatched: {len(x['mismatched'])}  "
          f"not_found_at_exchange: {len(x['not_found_at_exchange'])}")
    print(f"unclaimed_filled_orders: {len(x['unclaimed_filled_orders'])} "
          f"(during bot activity: {len(x['unclaimed_during_bot_activity'])})")
    print(f"account_had_prior_activity: {x['account_had_prior_activity']}")
    print(f"history_scan_complete: {x['history_complete']}  "
          f"archive_scan_complete: {x['archive_complete']}")
    for row in t.get("later_baseline_drift", []):
        name = Path(row["path"]).parent.name
        print(f"  baseline {name}: absorbed_prior_bot_base={row['absorbed_prior_bot_base']} "
              f"absorbed_prior_bot_spend={row['absorbed_prior_bot_spend']}")
    for row in t.get("conservation", []):
        print(f"conservation {row['ccy']}: match={row['match']} delta={row['delta']}")
    for row in t.get("untouched_gift", []):
        print(f"untouched {row['ccy']}: match={row['match']}")
    print(f"report: {out_file.relative_to(_ROOT).as_posix()} (Git-ignored)")


def main():
    all_exchange = [
        v for v in audit.scan_runs(_ROOT / "runs")
        if audit.identity_of(v).get("environment") == EXCHANGE_ENV
        and audit.account_of(v)
    ]
    # Only independent ledgers count towards the union: a migration copy restates
    # orders that already exist, so including it would double-count attempts and
    # activity. Derived ledgers are still listed, just not summed.
    views = [v for v in all_exchange if not audit.is_derived(v)]
    derived = [v for v in all_exchange if audit.is_derived(v)]
    others = [
        v for v in audit.scan_runs(_ROOT / "runs")
        if v["path"] not in {x["path"] for x in all_exchange}
    ]
    exchange = fetch_exchange(build_client())
    findings = analyse(views, derived, others, exchange)

    out_dir = _ROOT / "runs" / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"ledger-audit-{int(time.time())}.json"
    report = {
        "generated_at": time.time(),
        "exchange": {
            "cash": {k: str(v) for k, v in exchange["cash"].items()},
            "avail": {k: str(v) for k, v in exchange["avail"].items()},
            "order_count": len(exchange["orders"]),
        },
        "findings": findings,
        "directories": [audit.serialize(v) for v in views],
    }
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n")
    summarize(findings, out_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
