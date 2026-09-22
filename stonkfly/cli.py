"""Command parsing and dispatch. Execution defaults to paper trading."""

import argparse
import json
import math
from pathlib import Path

from .config import ALLOWED_PRODUCTS, Settings


_REPO_ROOT = Path(__file__).resolve().parent.parent


def build_parser():
    parser = argparse.ArgumentParser(prog="stonkfly")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--reuse-doomfly", type=Path)
    sub.add_parser("verify")

    run = sub.add_parser("run")
    run.add_argument("--live", action="store_true")
    run.add_argument("--okx", action="store_true", help="Use OKX public market data")
    run.add_argument("--okx-demo", action="store_true", help="OKX demo execution; requires --okx")
    run.add_argument("--preflight-only", action="store_true", help="Check startup without submitting orders")
    run.add_argument(
        "--max-order-attempts", type=int,
        help="Persisted submission budget per run directory; 0 = unbounded. Required for OKX demo execution.",
    )
    run.add_argument(
        "--allow-attempt-limit-change", action="store_true",
        help="Explicitly change the recorded attempt limit; consumed attempts are kept",
    )
    run.add_argument(
        "--adjudicate-absent", action="append", default=[], metavar="CLORDID",
        help="Operator adjudication of one UNKNOWN intent after complete account/order checks. Review with tools/okx_unknown_audit.py first.",
    )
    run.add_argument(
        "--migrate-protocol", action="store_true",
        help="Record adoption of changed source/settings; preserve money, attempts and checkpoint",
    )
    run.add_argument(
        "--resume-reviewed", action="store_true",
        help="After review and reconciliation, clear a transient halt",
    )
    run.add_argument("--fixture", action="store_true", help="Synthetic offline market; paper only")
    run.add_argument("--steps", type=int, default=0, help="0 keeps running")
    run.add_argument("--fast", action="store_true", help="Skip paper-mode waits; keep execution cooldown")
    run.add_argument("--frozen", action="store_true", help="Freeze memory efficacies for a control run")
    run.add_argument("--out", type=Path)
    run.add_argument("--products", nargs="+", choices=ALLOWED_PRODUCTS)
    run.add_argument("--neural-ms", type=float, default=500)
    run.add_argument(
        "--reward-anchor", choices=["tick", "trade"], default="tick",
        help="Equity reference: each tick, or after fills and on the configured horizon",
    )
    run.add_argument("--reward-horizon-ticks", type=int, default=1)
    run.add_argument(
        "--show-portfolio-state", action="store_true",
        help="Draw the bot's equity history and cash fraction into its observed frame",
    )

    status = sub.add_parser("status")
    watch = sub.add_parser("watch", help="Read-only terminal view of fly decisions")
    serve = sub.add_parser("serve", help="Read-only web view of fly decisions")
    for command in (status, watch, serve):
        command.add_argument("--out", type=Path, default=_REPO_ROOT / "runs" / "paper")
    watch.add_argument("--replay", action="store_true", help="Replay recorded ticks")
    watch.add_argument("--all", action="store_true", help="Replay from the first recorded tick")
    watch.add_argument("--speed", type=float, default=3.0, help="Replay ticks per second")
    serve.add_argument("--host", default="127.0.0.1", help="Bind address; 0.0.0.0 explicitly exposes the LAN")
    serve.add_argument("--port", type=int, default=8400)
    return parser


def _run_settings(args, parser):
    if args.live and (args.fixture or args.fast):
        parser.error("Live mode forbids fixtures and fast replay")
    if args.live and args.okx_demo:
        parser.error("--live (Coinbase) and --okx-demo are mutually exclusive")
    if args.live and args.okx:
        parser.error("--live (Coinbase) uses Coinbase market data; cannot combine with --okx")
    if args.okx and args.fixture:
        parser.error("--okx and --fixture are mutually exclusive")
    if args.okx_demo and not args.okx:
        parser.error("--okx-demo requires --okx market data")
    if args.okx_demo and (args.fixture or args.fast):
        parser.error("OKX demo trading forbids fixtures and fast replay")
    if args.steps < 0:
        parser.error("steps cannot be negative")
    if args.max_order_attempts is not None and args.max_order_attempts < 0:
        parser.error("--max-order-attempts cannot be negative")
    if args.okx_demo and args.max_order_attempts is None and not args.preflight_only:
        parser.error("--okx-demo requires --max-order-attempts N (0 = unbounded continuous run)")
    if (args.adjudicate_absent or args.migrate_protocol) and not args.okx_demo:
        parser.error("--adjudicate-absent/--migrate-protocol are only meaningful with --okx-demo")
    if any(not (cid.isalnum() and len(cid) <= 32) for cid in args.adjudicate_absent):
        parser.error("--adjudicate-absent expects OKX client order ids")
    if args.products is None:
        args.products = ["BTC-USDT"] if args.okx_demo else ["BTC-USDC"]
    return Settings(
        products=tuple(args.products), learning=not args.frozen,
        neural_ms=args.neural_ms, pulse_ms=min(200, args.neural_ms),
        reward_anchor=args.reward_anchor, reward_horizon_ticks=args.reward_horizon_ticks,
        show_portfolio_state=args.show_portfolio_state,
    )


def _print_status(out):
    from .watch import read_health, read_meta, read_pending_count

    meta = read_meta(out)
    identity = meta.get("identity") or {}
    limit, used = meta.get("attempt_limit"), meta.get("order_attempts")
    status = {key: meta.get(key) for key in (
        "mode", "tick", "cash", "positions", "initial_cash", "anchor", "halted",
        "order_attempts", "attempt_limit", "resolved_orders", "protocol_migrations",
        "algo_coverage_last",
    )}
    status.update(
        exchange=identity.get("exchange"), environment=identity.get("environment"),
        account_bound=bool(identity.get("account")), quote_ccy=identity.get("quote_ccy"),
        unresolved_orders=read_pending_count(out),
        attempts_remaining=None if not limit else max(0, limit - (used or 0)),
        algo_coverage_gap_ack_retired=meta.get("algo_coverage_gap_ack"),
        health=read_health(out),
    )
    print(json.dumps(status, indent=2))


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    from dotenv import load_dotenv

    # Scheduled launches may start outside the repo; never search parent .env files.
    load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=False)
    if args.command in ("prepare", "verify"):
        from .data import prepare, verify

        if args.command == "prepare":
            prepare(args.reuse_doomfly)
        else:
            print(json.dumps(verify()))
        return
    if args.command == "status":
        return _print_status(args.out)
    if args.command == "watch":
        from .watch import run

        if not math.isfinite(args.speed) or args.speed <= 0:
            parser.error("--speed must be positive and finite")
        return run(args.out, replay=args.replay, show_all=args.all, speed=args.speed)
    if args.command == "serve":
        from .web import serve

        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        return serve(args.out, args.host, args.port)

    settings = _run_settings(args, parser)
    mode = "okx-demo" if args.okx_demo else "live" if args.live else "paper"
    args.out = args.out or _REPO_ROOT / "runs" / mode
    from .runner import run

    return run(args, settings, mode)


if __name__ == "__main__":
    main()
