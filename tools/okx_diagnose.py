"""Diagnostic OKX demo probe (NOT the formal preflight).

This tool exists to answer "can this machine and these credentials reach OKX
demo, and is the account shape usable?" without touching a real run directory.
It uses a throwaway temporary ledger, so it neither reads nor initializes any
persisted run state and is **not** a substitute for the formal preflight, for an
automatic run, or for restart/acceptance verification.

The formal path uses the same configuration and a persistent ledger:

    .venv\\Scripts\\python.exe -m stonkfly run --okx --okx-demo --preflight-only \\
        --out runs\\okx-demo-accept --max-order-attempts 2

Run this diagnostic from the project root, passing the proxy as an argument
(never hardcoded):

    .venv\\Scripts\\python.exe tools\\okx_diagnose.py 127.0.0.1:7890

Credentials are loaded from ``.env`` and never printed. Output is limited to
stable error categories, OKX business codes, HTTP status codes, public ordType
names, and yes/no conclusions. No account id, key, secret, passphrase,
signature, balance amount or response body is ever printed.
"""

import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stonkfly.config import Settings  # noqa: E402
from stonkfly.ledger import Ledger  # noqa: E402
from stonkfly.okx_broker import (  # noqa: E402
    OKXAlgoCoverageUnverified,
    OKXBroker,
    OKXRiskError,
)
from stonkfly.okx_client import (  # noqa: E402
    OKXBusinessError,
    OKXClient,
    OKXTransportError,
)


def classify(e):
    """A stable category for a failure, with no free-form exchange text."""
    if isinstance(e, OKXAlgoCoverageUnverified):
        return "algo_coverage_unverified"
    if isinstance(e, OKXRiskError):
        return "account_risk_not_verified"
    if isinstance(e, OKXTransportError):
        return "transport_error"
    if isinstance(e, OKXBusinessError):
        return "okx_business_error"
    return "unexpected_error"


def report(e):
    print("success: false")
    print(f"error_category: {classify(e)}")
    print(f"error_type: {type(e).__name__}")
    if isinstance(e, OKXTransportError):
        if e.status is not None:
            print(f"http_status: {e.status}")
        else:
            cause = e.__cause__
            print(f"transport_cause: {type(cause).__name__ if cause else 'unknown'}")
    if isinstance(e, OKXBusinessError) and e.code:
        print(f"okx_code: {e.code}")
    for ord_type, code in getattr(e, "unverified", ()) or ():
        print(f"unverified_ordtype: {ord_type} okx_code={code}")



def main(argv):
    if len(argv) != 2 or not argv[1]:
        print("usage: python tools/okx_diagnose.py <proxy-host:port>")
        return 2

    proxy = argv[1]
    load_dotenv(dotenv_path=Path(_ROOT) / ".env", override=False)
    api_key = os.environ.get("OKX_API_KEY")
    secret = os.environ.get("OKX_API_SECRET")
    passphrase = os.environ.get("OKX_API_PASSPHRASE")
    if not (api_key and secret and passphrase):
        print("success: false")
        print("error_category: missing_credentials")
        print("error_detail: set OKX_API_KEY/SECRET/PASSPHRASE in .env")
        return 1

    client = OKXClient(
        api_key=api_key, secret=secret, passphrase=passphrase, demo=True,
        proxy=proxy,
    )
    settings = Settings(products=("BTC-USDT",))
    with tempfile.TemporaryDirectory() as td:
        ledger = Ledger(Path(td) / "ledger.sqlite", settings, "okx-demo")
        try:
            broker = OKXBroker(settings, ledger, client)
            result = broker.preflight()
        except Exception as e:  # noqa: BLE001 — only the sanitized report is printed
            report(e)
            ledger.close()
            return 1
        print("success: true")
        print("ledger: temporary (diagnostic only)")
        print(f"account_level: {result.get('account_level')}")
        print(f"account_bound: {result.get('account_bound')}")
        print(f"quote_ccy: {result.get('quote_ccy')}")
        print(f"spot_cash_mode: {result.get('spot_cash_mode')}")
        print(f"withdrawals_disabled: {result.get('withdrawals_disabled')}")
        print(f"algo_coverage: {result.get('algo_coverage')}")
        print(f"algo_coverage_source: {result.get('algo_coverage_source')}")
        baseline = ledger.get("baseline") or {}
        print(f"baseline_ccys: {','.join(sorted(baseline))}")
        ledger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
