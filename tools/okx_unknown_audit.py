"""Read-only investigation of UNKNOWN intents in one run directory.

This tool gathers evidence and stops there. It never changes a ledger, never
concludes that an order does not exist, and is not part of the automated path:
``reconcile``, ``--resume-reviewed`` and a normal restart all leave an UNKNOWN
intent open. Closing one is a separate, explicitly named human adjudication
(``run --adjudicate-absent CLORDID``) which re-collects this same evidence and
requires the account identity and balances to still reconcile first.

    .venv\\Scripts\\python.exe tools\\okx_unknown_audit.py runs\\okx-demo-restart

Reads credentials from ``.env`` for read-only queries only. The full evidence,
including exact order identifiers, is written to a Git-ignored report under
``runs/audit/``; the terminal output is sanitized.

What the evidence can and cannot establish -- documented in docs/okx.md -- is
summarised per intent so a human can judge it, not so the tool can.
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
from stonkfly.okx_broker import adjudication_basis, investigate_unknown  # noqa: E402
from stonkfly.okx_client import demo_client_from_env  # noqa: E402


def build_client():
    key = os.environ.get("OKX_API_KEY")
    secret = os.environ.get("OKX_API_SECRET")
    passphrase = os.environ.get("OKX_API_PASSPHRASE")
    if not (key and secret and passphrase):
        raise RuntimeError("Set OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE in .env")
    return demo_client_from_env(timeout=25)


def main(argv):
    if len(argv) != 2 or not argv[1]:
        print("usage: python tools/okx_unknown_audit.py <run-directory>")
        return 2
    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
    target = Path(argv[1])
    view = audit.read_ledger(target)
    if audit.identity_of(view).get("environment") != "okx-demo":
        print("refusing: this is not an okx-demo run directory")
        return 1

    unknown = [o for o in audit.pending(view) if o["status"] == "UNKNOWN"]
    client = build_client() if unknown else None
    report = {
        "run_directory": str(target),
        "generated_at": time.time(),
        "account_bound": bool(audit.account_of(view)),
        "attempts_used": audit.attempts_used(view),
        "attempt_limit": view["meta"].get("attempt_limit"),
        "retired_acknowledgements": view["meta"].get("algo_coverage_gap_ack"),
        "prior_adjudications": audit.adjudications(view),
        "intents": [],
    }
    now = time.time()
    for order in unknown:
        plan = order["plan"]
        findings = investigate_unknown(
            client, order["id"], plan.get("product"), order["created"], now=now
        )
        admissible, basis = adjudication_basis(plan, order["created"], findings)
        report["intents"].append(
            {
                "client_order_id": order["id"],
                "product": plan.get("product"),
                "side": plan.get("side"),
                "base_size": plan.get("base_size"),
                "order_type": plan.get("order_type"),
                "created": order["created"],
                "age_hours": round(findings["age_seconds"] / 3600, 3),
                "visible_at_exchange": basis.startswith("the order is visible"),
                "adjudication_admissible": admissible,
                "basis": basis,
                "findings": findings,
            }
        )

    out_dir = _ROOT / "runs" / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"unknown-{target.name}-{int(now)}.json"
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n")

    # Sanitized terminal summary: prefixes, counts, conclusions only.
    print(f"run_directory: {target.name}")
    print(f"unknown_intents: {len(report['intents'])}")
    for item in report["intents"]:
        print(f"  intent {item['client_order_id'][:8]}… product={item['product']} "
              f"side={item['side']} age={item['age_hours']}h")
        print(f"    visible_at_exchange: {item['visible_at_exchange']}")
        print(f"    adjudication_admissible: {item['adjudication_admissible']}")
        print(f"    basis: {item['basis']}")
    if report["prior_adjudications"]:
        print("prior_resolutions:")
        for entry in report["prior_adjudications"]:
            print(f"  {entry['id'][:8]}… source={entry.get('source', 'unknown')} "
                  f"evidence_recorded={entry['evidence_recorded']}")
    print(f"report: {out_file.relative_to(_ROOT).as_posix()} (Git-ignored)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
