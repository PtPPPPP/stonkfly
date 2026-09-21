"""Single-worker run loop. Default execution is paper; live must be explicit."""

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

from .config import D, Settings
from .locking import acquire as _acquire_worker_lock
from .locking import release as _release_worker_lock


def _attempt_budget_record(ledger):
    """The immutable attempt-budget configuration, for the provenance record.

    The attempts already spent are deliberately absent: provenance hashes the
    run protocol, and a mutable counter in it makes every restart of a directory
    that has traded look like a protocol change. The count is in ``status``.
    """
    return {"limit": ledger.get("attempt_limit")}


# The repository root anchors every default path (.env, runs/): a scheduled
# task may start this process with cwd=C:\Windows\System32, and depending on
# the process working directory would silently read the wrong (or no) .env and
# create run directories outside the project.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _append_event(out, row):
    """Append one event line, flushed and fsync'd before we continue."""
    with (out / "events.jsonl").open("a") as f:
        f.write(json.dumps(row, allow_nan=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _needs_warmup(history_len, settings):
    """True when the rendered chart would be missing candles the fly should
    see: market_frame draws history[-100:], so fewer candles than
    ``warmup_candles`` mean the decoder input is only partially rendered."""
    return history_len < settings.warmup_candles


def _maybe_rotate_events(out, settings, warn=print):
    """Rotate events.jsonl by size, keeping N generations.

    The writer opens/appends/closes per event, so no handle is held across
    calls and the rename can go through. A reader (watch/serve) that happens
    to hold the file open makes the rename fail on Windows -- that is logged
    and simply retried on the next tick; it can never kill the worker or lose
    the event being written.
    """
    path = out / "events.jsonl"
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < settings.events_rotate_mb * 1024 * 1024:
        return
    try:
        for generation in range(settings.events_keep - 1, 0, -1):
            src = out / f"events.{generation}.jsonl"
            if src.exists():
                src.replace(out / f"events.{generation + 1}.jsonl")
        path.replace(out / "events.1.jsonl")
        print(json.dumps({"events_rotated": True, "bytes": size}), flush=True)
    except OSError as e:
        warn(f"events rotation failed (will retry next tick): {type(e).__name__}")


def _sleep_until(until, stop_file):
    """Idle until the cadence deadline, waking every second to watch STOP."""
    while time.monotonic() < until and not stop_file.exists():
        time.sleep(min(1, until - time.monotonic()))


# Committed ticks between application-layer clock refreshes (~1 hour at the
# 60 s cadence). Operational cadence, not part of the run protocol.
_CLOCK_RECHECK_TICKS = 60


def main():
    p = argparse.ArgumentParser(prog="stonkfly")
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--reuse-doomfly", type=Path)
    sub.add_parser("verify")
    run = sub.add_parser("run")
    run.add_argument("--live", action="store_true")
    run.add_argument(
        "--okx",
        action="store_true",
        help="Use OKX public market data instead of Coinbase",
    )
    run.add_argument(
        "--okx-demo",
        action="store_true",
        help="Execute against OKX demo trading (requires --okx; paper is default)",
    )
    run.add_argument(
        "--preflight-only",
        action="store_true",
        help="Read-only exchange checks; never submit an order",
    )
    run.add_argument(
        "--max-order-attempts",
        type=int,
        default=None,
        help=(
            "Maximum number of order submission attempts this run directory may "
            "ever make; 0 means unbounded (continuous operation). Required with "
            "--okx-demo so bounded acceptance and continuous running are chosen "
            "explicitly. The count is persisted before a request can be sent."
        ),
    )
    run.add_argument(
        "--allow-attempt-limit-change",
        action="store_true",
        help="Change the attempt limit recorded in an existing run directory (used count is kept)",
    )
    run.add_argument(
        "--adjudicate-absent",
        action="append",
        default=[],
        metavar="CLORDID",
        help=(
            "Close ONE named UNKNOWN intent as a human adjudication. Requires the "
            "account identity and balance reconciliation to pass first, then a "
            "complete read-only investigation showing no fill and no resting order. "
            "Refuses if the order is visible anywhere. Run "
            "tools/okx_unknown_audit.py first to review the evidence. The audit "
            "record states that this is an operator judgement, not exchange "
            "confirmation."
        ),
    )
    run.add_argument(
        "--migrate-protocol",
        action="store_true",
        help=(
            "Adopt the current source/protocol signature for an existing run "
            "directory, recording the change in an append-only trail. Money state, "
            "budget, consumed attempts and the checkpoint are untouched, and every "
            "risk check still runs."
        ),
    )
    run.add_argument(
        "--resume-reviewed",
        action="store_true",
        help="After manual review, clear a transient halt only after successful reconciliation",
    )
    run.add_argument(
        "--fixture",
        action="store_true",
        help="Synthetic offline market input; paper only",
    )
    run.add_argument("--steps", type=int, default=0, help="0 keeps running")
    run.add_argument(
        "--fast",
        action="store_true",
        help="Skip waiting in paper mode; execution cooldown still applies",
    )
    run.add_argument(
        "--frozen",
        action="store_true",
        help="Freeze all memory efficacies for a control run",
    )
    run.add_argument("--out", type=Path)
    run.add_argument(
        "--products",
        nargs="+",
        default=None,
        choices=["BTC-USDC", "ETH-USDC", "SOL-USDC", "BTC-USDT", "ETH-USDT", "SOL-USDT"],
    )
    run.add_argument("--neural-ms", type=float, default=500)
    run.add_argument(
        "--reward-anchor",
        choices=["tick", "trade"],
        default="tick",
        help=(
            "Reward anchoring: 'tick' re-anchors every tick (per-tick equity "
            "change); 'trade' re-anchors at settled trades (and every "
            "--reward-horizon-ticks), so each pulse grades the previous action"
        ),
    )
    run.add_argument(
        "--reward-horizon-ticks",
        type=int,
        default=1,
        help="Fallback re-anchor cadence for --reward-anchor trade",
    )
    run.add_argument(
        "--show-portfolio-state",
        action="store_true",
        help="Draw the bot's own equity curve and cash-fraction bar into the observed frame",
    )
    status = sub.add_parser("status")
    status.add_argument("--out", type=Path, default=_REPO_ROOT / "runs" / "paper")
    watch = sub.add_parser(
        "watch", help="Read-only terminal view of a run's fly decisions"
    )
    watch.add_argument("--out", type=Path, default=_REPO_ROOT / "runs" / "paper")
    watch.add_argument(
        "--replay",
        action="store_true",
        help="Play back recorded ticks instead of following live output",
    )
    watch.add_argument(
        "--all",
        action="store_true",
        help="With --replay, start from the first recorded tick",
    )
    watch.add_argument(
        "--speed", type=float, default=3.0, help="Replay ticks per second"
    )
    serve = sub.add_parser(
        "serve", help="Read-only LAN web view of a run's fly decisions"
    )
    serve.add_argument("--out", type=Path, default=_REPO_ROOT / "runs" / "paper")
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Bind address; 127.0.0.1 (default) serves only this machine. "
            "Pass 0.0.0.0 explicitly to serve the LAN."
        ),
    )
    serve.add_argument("--port", type=int, default=8400)
    a = p.parse_args()
    from dotenv import load_dotenv

    # Never search parent projects for unrelated account credentials, and
    # never depend on the process working directory (a scheduled task may run
    # from C:\Windows\System32): the repository's own .env is the only one read.
    load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=False)
    if a.command in ("prepare", "verify"):
        from .data import prepare, verify

        if a.command == "prepare":
            prepare(a.reuse_doomfly)
        else:
            print(json.dumps(verify()))
        return
    if a.command == "status":
        from .watch import read_meta, read_pending_count

        meta = read_meta(a.out)
        ident = meta.get("identity") or {}
        limit = meta.get("attempt_limit")
        used = meta.get("order_attempts")
        pending = read_pending_count(a.out)
        from .watch import read_health

        health = read_health(a.out)
        # No account identifier is printed: it identifies the exchange account
        # and is not needed to inspect local run state.
        print(
            json.dumps(
                {
                    "mode": meta.get("mode"),                    "exchange": ident.get("exchange"),
                    "environment": ident.get("environment"),
                    "account_bound": bool(ident.get("account")),
                    "quote_ccy": ident.get("quote_ccy"),
                    "tick": meta.get("tick"),
                    "cash": meta.get("cash"),
                    "positions": meta.get("positions"),
                    "initial_cash": meta.get("initial_cash"),
                    "anchor": meta.get("anchor"),
                    "halted": meta.get("halted"),
                    "unresolved_orders": pending,
                    "order_attempts": used,
                    "attempt_limit": limit,
                    "attempts_remaining": None if not limit else max(0, limit - (used or 0)),
                    # Retired control: kept visible as history, never consulted.
                    "algo_coverage_gap_ack_retired": meta.get("algo_coverage_gap_ack"),
                    "resolved_orders": meta.get("resolved_orders"),
                    "protocol_migrations": meta.get("protocol_migrations"),
                    "algo_coverage_last": meta.get("algo_coverage_last"),
                    "health": health,
                },
                indent=2,
            )
        )
        return
    if a.command == "watch":
        from .watch import run as run_watch

        if a.speed <= 0:
            p.error("--speed must be positive")
        return run_watch(a.out, replay=a.replay, show_all=a.all, speed=a.speed)
    if a.command == "serve":
        from .web import serve as serve_web

        if not 1 <= a.port <= 65535:
            p.error("--port must be between 1 and 65535")
        return serve_web(a.out, a.host, a.port)
    if a.live and (a.fixture or a.fast):
        p.error("Live mode forbids fixtures and fast replay")
    if a.live and a.okx_demo:
        p.error("--live (Coinbase) and --okx-demo are mutually exclusive")
    if a.live and a.okx:
        p.error("--live (Coinbase) uses Coinbase market data; cannot combine with --okx")
    if a.okx and a.fixture:
        p.error("--okx and --fixture are mutually exclusive")
    if a.okx_demo and not a.okx:
        p.error("--okx-demo requires --okx market data")
    if a.okx_demo and (a.fixture or a.fast):
        p.error("OKX demo trading forbids fixtures and fast replay")
    if a.steps < 0:
        p.error("steps cannot be negative")
    if a.max_order_attempts is not None and a.max_order_attempts < 0:
        p.error("--max-order-attempts cannot be negative")
    if a.okx_demo and a.max_order_attempts is None and not a.preflight_only:
        # A read-only preflight submits nothing, so it must be able to check an
        # account without also binding the submission policy of a future run.
        p.error(
            "--okx-demo requires --max-order-attempts N (0 = unbounded continuous "
            "run) so a bounded acceptance run is never confused with continuous "
            "operation"
        )
    if (a.adjudicate_absent or a.migrate_protocol) and not a.okx_demo:
        p.error("--adjudicate-absent/--migrate-protocol are only meaningful with --okx-demo")
    if a.adjudicate_absent:
        bad = [c for c in a.adjudicate_absent if not (c.isalnum() and len(c) <= 32)]
        if bad:
            p.error("--adjudicate-absent expects OKX client order ids")
    if a.products is None:
        a.products = ["BTC-USDT"] if a.okx_demo else ["BTC-USDC"]
    settings = Settings(
        products=tuple(a.products),
        learning=not a.frozen,
        neural_ms=a.neural_ms,
        pulse_ms=min(200, a.neural_ms),
        reward_anchor=a.reward_anchor,
        reward_horizon_ticks=a.reward_horizon_ticks,
        show_portfolio_state=a.show_portfolio_state,
    )
    mode = "okx-demo" if a.okx_demo else ("live" if a.live else "paper")
    feed = "fixture" if a.fixture else ("okx" if a.okx else "coinbase-public")
    if a.okx_demo:
        identity = {
            "exchange": "okx",
            "environment": "okx-demo",
            "account": None,
            "quote_ccy": settings.quote_ccy,
        }
    elif a.live:
        identity = {
            "exchange": "coinbase",
            "environment": "live",
            "account": None,
            "quote_ccy": settings.quote_ccy,
        }
    else:
        identity = {
            "exchange": "paper",
            "environment": "paper",
            "account": None,
            "quote_ccy": settings.quote_ccy,
        }
    out = a.out or _REPO_ROOT / "runs" / (
        "live" if a.live else ("okx-demo" if a.okx_demo else "paper")
    )
    out.mkdir(parents=True, exist_ok=True)
    try:
        lock = _acquire_worker_lock(out / "worker.lock")
    except BlockingIOError:
        raise SystemExit("A worker already owns this run directory")
    from .broker import CoinbaseBroker, PaperBroker
    from .ledger import Ledger

    try:
        ledger = Ledger(out / "ledger.sqlite", settings, mode, identity=identity,
                        adopt_settings=a.migrate_protocol)
    except Exception as e:
        # Refusals at open (settings/protocol/mode/identity mismatch, database
        # damage) leave the directory exactly as it was: report the reason
        # cleanly instead of a raw traceback, and release the still-held lock.
        _release_worker_lock(lock)
        print(f"Run directory refused this start: {e}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    try:
        if a.max_order_attempts is not None:
            # Persisted per run directory, so a restart keeps the same budget and
            # the attempts already spent.
            ledger.set_attempt_limit(
                a.max_order_attempts, allow_change=a.allow_attempt_limit_change
            )
        if a.okx_demo:
            from .okx_broker import OKXBroker

            broker = OKXBroker.from_env(settings, ledger)
        else:
            broker = (
                CoinbaseBroker.from_env(settings, ledger)
                if a.live
                else PaperBroker(settings, ledger)
            )
        if a.adjudicate_absent:
            # Before preflight, which would otherwise refuse to reconcile an
            # uncertain intent. Each named intent is verified and judged on its
            # own evidence; nothing is closed on absence alone.
            records = [
                broker.adjudicate_absent(cid) for cid in a.adjudicate_absent
            ]
            print(
                json.dumps(
                    {
                        "adjudicated_absent": [
                            {
                                "client_order_id": r["id"],
                                "source": r["source"],
                                "confirmation": r["confirmation"],
                            }
                            for r in records
                        ]
                    }
                ),
                flush=True,
            )
        from .okx_client import is_transient_read_failure

        def _preflight_transient_exit(exc):
            """Exit on a transient read-only preflight failure WITHOUT halting.

            Nothing in this process has traded and no intent was touched: the
            run state is exactly as before the attempt, so the next scheduled
            launch can simply retry the same checks. Halting here would turn a
            proxy blip during an unattended restart into a permanent stop.
            """
            frames = traceback.extract_tb(exc.__traceback__)
            (out / "error.json").write_text(
                json.dumps(
                    {
                        "type": type(exc).__name__,
                        "reason": "Transient read-only failure during preflight; "
                        "run state unchanged and not halted; the next launch retries.",
                        "locations": [
                            f"{Path(f.filename).name}:{f.lineno} {f.name}"
                            for f in frames
                        ],
                    },
                    indent=2,
                )
                + "\n"
            )
            print(
                "Preflight hit a transient read-only failure; nothing was started "
                "and the ledger was not halted. The next launch will retry.",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(1) from None

        # Sanitized: the broker returns no account identifier or balance.
        try:
            result = broker.preflight()
        except Exception as e:
            if is_transient_read_failure(e):
                _preflight_transient_exit(e)
            raise
        print(json.dumps(result), flush=True)
        ledger.put(
            "algo_coverage_last",
            {
                "at": time.time(),
                "coverage": result.get("algo_coverage"),
                "window_days": result.get("algo_coverage_window_days"),
                "cross_checked": result.get("algo_coverage_cross_checked"),
                "types": result.get("algo_coverage_types"),
            },
        )

        # Application-layer clock calibration against OKX public time (read
        # only, several samples, median of the lowest-RTT half). The offset
        # adjusts the guard's freshness/cooldown clock; the Windows system
        # clock is never modified. Beyond the configured limit the quote-age
        # window cannot be trusted, so the run refuses to start instead.
        calibrated_client = None
        clock_offset = {"value": 0.0}

        def clock():
            return time.time() + clock_offset["value"]

        if a.okx and not a.fixture:
            if a.okx_demo:
                calibrated_client = broker.client
            else:
                from .okx_client import OKXClient

                calibrated_client = OKXClient(
                    api_key=None, secret=None, passphrase=None
                )
            try:
                measured = calibrated_client.measure_time_offset()
            except Exception as e:
                if is_transient_read_failure(e):
                    _preflight_transient_exit(e)
                raise
            print(
                json.dumps(
                    {
                        "clock_offset_seconds": round(measured["offset"], 3),
                        "clock_rtt_seconds": round(measured["rtt"], 3),
                        "clock_samples": len(measured["samples"]),
                    }
                ),
                flush=True,
            )
            if abs(measured["offset"]) > settings.clock_offset_max:
                raise RuntimeError(
                    f"Local clock is {measured['offset']:+.2f} s from OKX server "
                    f"time (limit ±{settings.clock_offset_max:g} s); sync the "
                    "Windows clock. Quote freshness cannot be judged at this "
                    "offset, so the run refuses to start."
                )
            clock_offset["value"] = measured["offset"]
            ledger.put("clock_offset", round(measured["offset"], 3))
        if a.resume_reviewed:
            if (out / "STOP").exists() or ledger.pending():
                raise RuntimeError(
                    "Remove STOP only after review; unresolved orders cannot resume"
                )
            reason = ledger.get("halted")
            if reason and ("Loss stop" in reason or "fee exceeded" in reason):
                raise RuntimeError("A financial stop cannot be cleared by this flag")
            ledger.put("halted", None)
        if a.max_order_attempts == 0 and (ledger.pending() or ledger.get("halted")):
            # Unbounded operation is only allowed from a fully resolved state.
            raise RuntimeError(
                "Continuous operation requires no unresolved intent and no halt"
            )
        if a.preflight_only and not a.migrate_protocol:
            # A plain preflight stays cheap: no dataset or brain is loaded.
            return
        from .data import verify

        verified = verify()
        from PIL import Image

        from .actions import StonkflyActions
        from .display import market_frame
        from .market import CoinbaseMarket, FixtureMarket
        from .okx_market import OKXMarket
        from .neural.controller import FlyController
        from .reinforcement import reinforcement
        from .risk import Guard, Veto

        market = (
            FixtureMarket(settings.products)
            if a.fixture
            else (OKXMarket(settings.products) if a.okx else CoinbaseMarket(settings.products))
        )
        previous = ledger.get("observation")
        if previous:
            market.history = previous["market_history"]
            if a.fixture:
                market.tick = previous["fixture_tick"]
        controller = FlyController(settings)
        cp = ledger.get("checkpoint")
        if cp:
            from .neural.brain import restore_verified

            restore_verified(controller, out, cp, ledger=ledger)
        provenance = {
            "settings": dataclasses.asdict(settings),
            "dataset": verified,
            "circuit": controller.brain.circuit["report"],
            "vision": controller.brain.visual_report,
            "mode": broker.mode,
            "feed": feed,
            # Bounded run vs continuous operation, and any acknowledged gap, are
            # part of the run's recorded protocol.
            "order_attempt_budget": _attempt_budget_record(ledger),
            "decoder": "DNp20 mean R-L: buy/sell; DNpe017 spike gate; otherwise hold. Engineered fixed mapping.",
            "learning_validated": False,
            "pain_receptors_modeled": False,
            "timing": "Each observation advances configured neural_ms regardless of wall-market time; no claim of real-time fly physiology.",
            "source_sha256": {
                str(path.relative_to(Path(__file__).parent)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in Path(__file__).parent.rglob("*")
                if path.suffix in (".py", ".cpp")
            },
        }
        signature = hashlib.sha256(
            json.dumps(provenance, sort_keys=True).encode()
        ).hexdigest()
        if ledger.get("provenance_sha256") not in (None, signature):
            if not a.migrate_protocol:
                # Make the mismatch self-diagnosing: name which components of
                # the recomputed provenance differ from the recorded one, by
                # hash only. Without this, a recurring mismatch is a mystery.
                diff = {}
                old_provenance = {}
                try:
                    old_provenance = json.loads(
                        (out / "provenance.json").read_text()
                    )
                except (OSError, ValueError):
                    pass
                for key in sorted(set(provenance) | set(old_provenance)):
                    old_hash = hashlib.sha256(
                        json.dumps(old_provenance.get(key), sort_keys=True,
                                   default=str).encode()
                    ).hexdigest()[:12]
                    new_hash = hashlib.sha256(
                        json.dumps(provenance.get(key), sort_keys=True,
                                   default=str).encode()
                    ).hexdigest()[:12]
                    if old_hash != new_hash:
                        diff[key] = f"{old_hash} -> {new_hash}"
                raise RuntimeError(
                    "Run source/protocol changed"
                    + (f"; components: {diff}" if diff else
                       " (recorded provenance unavailable for a component diff)")
                    + ". Review the change, then either use a separate run "
                    "directory or pass --migrate-protocol to adopt it in place "
                    "(money state, budget and attempts are kept)."
                )
            previous = ledger.migrate_protocol(
                signature, note="adopted by --migrate-protocol"
            )
            print(
                json.dumps(
                    {
                        "protocol_migrated": True,
                        "from": (previous or "")[:12],
                        "to": signature[:12],
                    }
                ),
                flush=True,
            )
        else:
            ledger.put("provenance_sha256", signature)
        (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        if a.preflight_only:
            # Reached only with --migrate-protocol (the plain preflight returned
            # before the heavy loads): the migration is recorded and the run
            # stops before any observation or submission. Falling through here
            # instead would start trading from a command that reads as a check.
            return
        guard = Guard(settings, ledger, out / "STOP", clock=clock)
        provider = StonkflyActions(guard, broker)
        action = provider.get_actions()[0]
        count = 0
        consecutive_read_failures = 0
        ticks_since_clock_recheck = 0
        # The clients whose read-only observation requests are bounded by the
        # per-tick read budget. Order submission and order queries never go
        # through this budget (place_order/get_order bypass _get).
        read_clients = []
        if a.okx_demo:
            read_clients.append(broker.client)
        elif a.okx and not a.fixture:
            read_clients.append(market.client)
        quote_label = settings.quote_ccy.lower()
        while not a.steps or count < a.steps:
            started = time.monotonic()
            stop_requested = (out / "STOP").exists()
            halted = ledger.get("halted")
            if stop_requested or halted:
                reason = "STOP file exists" if stop_requested else f"run is halted: {halted}"
                print(f"Run stopped before tick: {reason}", file=sys.stderr, flush=True)
                break
            _maybe_rotate_events(out, settings)
            broker.reconcile()
            read_started = time.monotonic()
            for client in read_clients:
                client.read_deadline = read_started + settings.tick_budget_seconds
                client.read_retries_used = 0
            try:
                component = "balance/risk sweep"
                broker.verify_balances()
                if ledger.attempts_remaining() == 0:
                    # Self-stopping: no STOP file and no operator attention needed.
                    print(
                        "Order attempt budget exhausted; no further submissions. "
                        "Submitted orders were reconciled and run state is preserved.",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
                component = "market data"
                quotes = market.snapshot()
            except Exception as e:
                for client in read_clients:
                    client.read_deadline = None
                # Whitelist, never a blanket catch: only bounded-retry-exhausted
                # failures of read-only requests (transport loss, retryable HTTP
                # statuses, OKX transient codes, unreadable market payloads) may
                # skip a tick. Program bugs, account-state errors and everything
                # touching order state re-raise into the halt path unchanged.
                # A skipped tick trades nothing, touches no intent, and never
                # reuses the stale data that triggered it; only after
                # read_fail_halt_after CONSECUTIVE skips does the run halt.
                if not is_transient_read_failure(e):
                    raise
                consecutive_read_failures += 1
                skip_row = {
                    "type": "tick_skipped",
                    "tick": ledger.get("tick"),
                    "wall_time": time.time(),
                    "component": component,
                    "consecutive": consecutive_read_failures,
                    "error": type(e).__name__,
                    "readonly_duration_ms": round((time.monotonic() - read_started) * 1000),
                    "deadline_exhausted": bool(getattr(e, "deadline", False)),
                    "read_retries": sum(c.read_retries_used for c in read_clients),
                }
                _append_event(out, skip_row)
                print(json.dumps(skip_row), flush=True)
                if consecutive_read_failures >= settings.read_fail_halt_after:
                    reason = (
                        f"Read-only data unavailable for "
                        f"{consecutive_read_failures} consecutive ticks "
                        f"(last component: {component})"
                    )
                    ledger.halt(reason)
                    raise RuntimeError(reason) from e
                if not a.fast and (not a.steps or count < a.steps):
                    _sleep_until(started + settings.interval_seconds, out / "STOP")
                continue
            readonly_ms = round((time.monotonic() - read_started) * 1000)
            for client in read_clients:
                client.read_deadline = None
            try:
                guard.check(quotes, clock())
            except Veto:
                # A STOP file can arrive after this loop's own check above has
                # already passed, in which case the guard is what refuses. That
                # is the operator's stop request, not an execution failure, and
                # nothing was submitted (the guard raises before any order), so
                # it must not leave a halt behind: the documented "drop STOP and
                # re-run" flow would otherwise silently need --resume-reviewed.
                # Every other guard refusal still halts, and any condition that
                # persists (an unresolved intent, the loss stop) is re-checked
                # and re-raises on the next start.
                if not (out / "STOP").exists():
                    raise
                print(
                    "Run stopped before tick: STOP file exists",
                    file=sys.stderr,
                    flush=True,
                )
                break
            market.record(quotes)
            product = settings.products[ledger.get("tick") % len(settings.products)]
            q = quotes[product]
            # Warmup: the rendered frame draws history[-100:], so with fewer
            # completed candles the fly would act on a chart it can only
            # partially see. Observe nothing, commit nothing, submit nothing:
            # this is a wait, never a veto or a halt, and spends no attempt.
            history_len = len(market.history[product])
            if _needs_warmup(history_len, settings):
                ledger.put(
                    "warmup",
                    {"candles": history_len, "required": settings.warmup_candles},
                )
                _append_event(
                    out,
                    {
                        "type": "warming_up",
                        "tick": ledger.get("tick"),
                        "wall_time": time.time(),
                        "candles": history_len,
                        "required": settings.warmup_candles,
                    },
                )
                print(
                    json.dumps(
                        {
                            "warming_up": True,
                            "candles": history_len,
                            "required": settings.warmup_candles,
                        }
                    ),
                    flush=True,
                )
                if not a.fast and (not a.steps or count < a.steps):
                    _sleep_until(started + settings.interval_seconds, out / "STOP")
                continue
            if ledger.get("warmup"):
                ledger.put("warmup", None)
            equity = ledger.equity(quotes)
            anchor = ledger.get("anchor")
            kind, delta = reinforcement(equity, anchor, settings.reward_deadband)
            previous = ledger.get("observation") or {}
            equity_history = [
                float(v) for v in (previous.get("equity_history") or [])
            ][-(120 - 1):] + [float(equity)]
            state = (
                {
                    "equity_history": equity_history[:-1],
                    "cash_ratio": float(ledger.cash / equity) if equity else 0.0,
                }
                if settings.show_portfolio_state
                else None
            )
            frame = market_frame(
                product, market.history[product], q.bid, q.ask, state=state
            )
            neural = controller.observe(frame, kind)
            # Checkpoint + accounting anchor are committed before any trade.
            # Two slots keep the last committed snapshot safe during a crash;
            # the previous slot + hash remain available for manual inspection.
            # Corruption stops the run; the brain cannot roll back alone.
            slot = ledger.get("tick") % 2
            checkpoint = out / f"brain-{slot}.npz"
            controller.save(checkpoint)
            previous_checkpoint = ledger.get("checkpoint") or {}
            checkpoint_info = {
                "file": checkpoint.name,
                "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
                "prev_file": previous_checkpoint.get("file"),
                "prev_sha256": previous_checkpoint.get("sha256"),
            }
            observation = {
                "neural": neural,
                "product": product,
                "quote": q.json(),
                f"pnl_delta_{quote_label}": str(delta),
                "market_history": market.history,
                "fixture_tick": getattr(market, "tick", None),
            }
            observation["equity_history"] = equity_history
            # "tick" mode re-anchors every tick; "trade" mode holds the anchor
            # between events (re-based after settled trades below, and on the
            # horizon cadence here) so each pulse grades the previous action.
            rebase = (
                settings.reward_anchor == "tick"
                or ledger.get("tick") % max(1, settings.reward_horizon_ticks) == 0
            )
            ledger.commit_tick(
                equity if rebase else None, checkpoint_info, observation
            )
            consecutive_read_failures = 0
            order = {"status": "HOLD"}
            if neural["side"] != "HOLD":
                try:
                    # Neural integration can be slow; use a fresh execution book.
                    fresh = market.snapshot()
                except Exception as e:
                    # This snapshot precedes any intent reservation, so nothing
                    # has been sent and nothing is left unresolved: refusing the
                    # action is final and safe. It is recorded as an execution
                    # veto instead of trading on the stale observation quote.
                    if not is_transient_read_failure(e):
                        raise
                    order = {
                        "status": "VETO",
                        "reason": (
                            "transient market-data failure before execution "
                            f"({type(e).__name__}); no order attempted"
                        ),
                    }
                else:
                    try:
                        latest = fresh[product]
                        if abs(latest.bid - q.bid) / q.bid > D(settings.slippage):
                            raise Veto(
                                "Price moved beyond neural observation tolerance"
                            )
                        provider.quotes = fresh
                        order = action.invoke(
                            {"product": product, "side": neural["side"]}
                        )
                    except Veto as e:
                        order = {"status": "VETO", "reason": str(e)}
            if (
                settings.reward_anchor == "trade"
                and isinstance(order.get("client_order_id"), str)
                and ledger.filled_trade(order["client_order_id"])
            ):
                # Trade-anchored reinforcement: the post-trade equity becomes
                # the reference the following ticks are graded against, so the
                # consequence of this action lands as one readable pulse. The
                # semantic test is the ledger settlement itself (base fill > 0),
                # so paper "FILLED" and exchange "SETTLED" are treated alike and
                # a zero-fill FOK cancellation never re-anchors.
                ledger.put("anchor", str(ledger.equity(provider.quotes)))
            row = {
                "tick": ledger.get("tick"),
                "wall_time": time.time(),
                "product": product,
                "mode": broker.mode,
                "quote": q.json(),
                f"equity_{quote_label}": str(equity),
                f"pnl_delta_{quote_label}": str(delta),
                "neural": neural,
                "execution": order,
                "readonly_duration_ms": readonly_ms,
                "read_retries": sum(c.read_retries_used for c in read_clients),
                "tick_duration_ms": round((time.monotonic() - started) * 1000),
            }
            _append_event(out, row)
            Image.fromarray(frame).save(out / "latest-input.png")
            (out / "latest.json").write_text(json.dumps(row, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "tick": row["tick"],
                        "side": neural["side"],
                        "execution": order["status"],
                        "equity": str(equity),
                        "stimulus": kind,
                        "plastic_edges_changed": neural["memory"]["changed_edges"],
                    }
                ),
                flush=True,
            )
            # Off the execution critical path: refresh the clock offset
            # occasionally so a slowly drifting local clock cannot silently
            # push the freshness window out of trust. A failed refresh keeps
            # the last validated offset; a drift beyond the configured limit
            # halts, because quote freshness could no longer be judged.
            ticks_since_clock_recheck += 1
            if (
                calibrated_client is not None
                and ticks_since_clock_recheck >= _CLOCK_RECHECK_TICKS
            ):
                ticks_since_clock_recheck = 0
                try:
                    remeasured = calibrated_client.measure_time_offset(samples=3)
                except Exception as e:
                    # A failed refresh keeps the last validated offset and is
                    # logged, never silent: the next re-check retries.
                    print(
                        json.dumps({"clock_recheck_failed": type(e).__name__}),
                        flush=True,
                    )
                else:
                    if abs(remeasured["offset"]) > settings.clock_offset_max:
                        raise RuntimeError(
                            f"Local clock drifted {remeasured['offset']:+.2f} s "
                            f"from OKX server time (limit "
                            f"±{settings.clock_offset_max:g} s); halting so "
                            "quote freshness cannot be trusted"
                        )
                    clock_offset["value"] = remeasured["offset"]
            count += 1
            if not a.fast and (not a.steps or count < a.steps):
                _sleep_until(started + settings.interval_seconds, out / "STOP")
    except KeyboardInterrupt:
        print("Stopped; run state preserved.", flush=True)
    except Exception as e:
        # Never print SDK exception text: it may contain account/request details.
        # A failure before this run directory had done any work (no tick, no order
        # intent) is a configuration or environment rejection, not an execution
        # state, so it must not leave a permanent halt behind that would poison an
        # otherwise untouched directory. Anything later halts as before.
        started = (
            ledger.get("tick")
            or ledger.pending()
            or ledger.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        )
        if started and not ledger.get("halted"):
            ledger.halt(type(e).__name__)
        frames = traceback.extract_tb(e.__traceback__)
        origin = frames[-1] if frames else None
        internal = origin and Path(origin.filename).is_relative_to(
            Path(__file__).parent
        )
        diagnostic = {
            "type": type(e).__name__,
            "reason": str(e)
            if internal
            else "External dependency error; review connection and account state.",
            "locations": [
                f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in frames
            ],
        }
        # An unverifiable algo-order coverage gap names the exact ordTypes, which
        # the operator needs in order to decide whether to accept it. ordType
        # names are public API vocabulary, not account data.
        unverified = list(getattr(e, "unverified", ()) or ())
        if unverified:
            diagnostic["algo_coverage_unverified"] = [
                {"ordType": t, "okx_code": c} for t, c in unverified
            ]
        # The exchange's own top-level code is public API vocabulary too, and it
        # is the only thing that makes an ambiguous submission diagnosable after
        # the fact. It carries no balance, identifier or response body.
        okx_code = getattr(e, "okx_code", None)
        if okx_code:
            diagnostic["okx_code"] = okx_code
        (out / "error.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        print(
            f"Stopped safely: {type(e).__name__}. Inspect local state and reconcile before restarting.",
            file=sys.stderr,
        )
        if unverified:
            print(
                "Untriggered algo order coverage unavailable for: "
                + ",".join(f"{t}(code={c})" for t, c in unverified)
                + ". No documented substitute exists; the run stays stopped.",
                file=sys.stderr,
            )
        raise SystemExit(1) from None
    finally:
        ledger.close()
        _release_worker_lock(lock)


if __name__ == "__main__":
    main()
