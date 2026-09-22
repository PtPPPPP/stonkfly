"""Single-worker lifecycle and observe/commit/execute ordering."""

import sys
import time
from contextlib import ExitStack, contextmanager
from enum import Enum, auto

from . import locking, run_state
from .config import D
from .okx_client import is_transient_read_failure
from .risk import Guard, Veto


_CLOCK_RECHECK_TICKS = 60


class TickResult(Enum):
    COMMITTED = auto()
    WAIT = auto()
    STOP = auto()


def needs_warmup(history_len, settings):
    return history_len < settings.warmup_candles


def sleep_until(until, stop_file):
    while not stop_file.exists():
        remaining = until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1, remaining))


@contextmanager
def observation_budget(clients, seconds):
    """Bound only observation reads; clear the budget on every exit path."""
    deadline = time.monotonic() + seconds
    for client in clients:
        client.read_deadline = deadline
        client.read_retries_used = 0
    try:
        yield
    finally:
        for client in clients:
            client.read_deadline = None


def run(args, settings, mode):
    from .ledger import Ledger

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    try:
        lock = locking.acquire(out / "worker.lock")
    except BlockingIOError:
        raise SystemExit("A worker already owns this run directory") from None
    # Register cleanup immediately: imports, failed startup and failed close
    # must all release the directory lock.
    with ExitStack() as cleanup:
        cleanup.callback(locking.release, lock)
        try:
            ledger = Ledger(out / "ledger.sqlite", settings, mode, adopt_settings=args.migrate_protocol)
        except Exception as error:
            print(f"Run directory refused this start: {error}", file=sys.stderr, flush=True)
            raise SystemExit(1) from None
        cleanup.callback(ledger.close)
        try:
            RunSession(args, settings, ledger).start()
        except KeyboardInterrupt:
            print("Stopped; run state preserved.", flush=True)
        except Exception as error:
            run_state.report_failure(out, ledger, error)
            raise SystemExit(1) from None


class RunSession:
    def __init__(self, args, settings, ledger):
        self.args = args
        self.settings = settings
        self.ledger = ledger
        self.out = args.out
        self.stop_file = self.out / "STOP"
        self.clock_client = None
        self.clock_offset = 0.0
        self.read_clients = []
        self.consecutive_read_failures = 0

    def now(self):
        return time.time() + self.clock_offset

    def start(self):
        from .broker import CoinbaseBroker, PaperBroker

        args, ledger = self.args, self.ledger
        if args.max_order_attempts is not None:
            ledger.set_attempt_limit(args.max_order_attempts, allow_change=args.allow_attempt_limit_change)
        if args.okx_demo:
            from .okx_broker import OKXBroker

            self.broker = OKXBroker.from_env(self.settings, ledger)
        elif args.live:
            self.broker = CoinbaseBroker.from_env(self.settings, ledger)
        else:
            self.broker = PaperBroker(self.settings, ledger)
        if args.adjudicate_absent:
            records = [self.broker.adjudicate_absent(cid) for cid in args.adjudicate_absent]
            run_state.emit({"adjudicated_absent": [
                {"client_order_id": row["id"], "source": row["source"], "confirmation": row["confirmation"]}
                for row in records
            ]})
        self._preflight()
        self._check_resume()
        if args.preflight_only and not args.migrate_protocol:
            return
        self._load_experiment()
        # Protocol migration may load the brain, but preflight must still stop
        # before any observation or submission.
        if not args.preflight_only:
            self._run_loop()

    def _preflight(self):
        from .okx_client import OKXClient

        try:
            result = self.broker.preflight()
            run_state.emit(result)
            self.ledger.put("algo_coverage_last", {
                "at": time.time(), "coverage": result.get("algo_coverage"),
                "window_days": result.get("algo_coverage_window_days"),
                "cross_checked": result.get("algo_coverage_cross_checked"),
                "types": result.get("algo_coverage_types"),
            })
            if self.args.okx:
                self.clock_client = (
                    self.broker.client if self.args.okx_demo else
                    OKXClient(api_key=None, secret=None, passphrase=None)
                )
                self._calibrate_clock(samples=5)
        except Exception as error:
            if is_transient_read_failure(error):
                run_state.preflight_read_failure(self.out, error)
            raise

    def _calibrate_clock(self, samples):
        measured = self.clock_client.measure_time_offset(samples=samples)
        offset = measured["offset"]
        if abs(offset) > self.settings.clock_offset_max:
            raise RuntimeError(
                f"Local clock is {offset:+.2f} s from OKX server time "
                f"(limit ±{self.settings.clock_offset_max:g} s); sync the system clock. "
                "Quote freshness cannot be trusted."
            )
        self.clock_offset = offset
        self.ledger.put("clock_offset", round(offset, 3))
        run_state.emit({
            "clock_offset_seconds": round(offset, 3),
            "clock_rtt_seconds": round(measured["rtt"], 3),
            "clock_samples": len(measured["samples"]),
        })

    def _refresh_clock(self):
        if self.clock_client is None:
            return
        try:
            self._calibrate_clock(samples=3)
        except Exception as error:
            # Only a classified read outage may retain the validated offset.
            # Bad data, clock drift and programming errors must stop execution.
            if not is_transient_read_failure(error):
                raise
            run_state.emit({"clock_recheck_failed": type(error).__name__})

    def _check_resume(self):
        ledger = self.ledger
        if self.args.resume_reviewed:
            if self.stop_file.exists() or ledger.pending():
                raise RuntimeError("Remove STOP only after review; unresolved orders cannot resume")
            reason = ledger.get("halted")
            if reason and ("Loss stop" in reason or "fee exceeded" in reason):
                raise RuntimeError("A financial stop cannot be cleared by this flag")
            ledger.put("halted", None)
        if self.args.max_order_attempts == 0 and (ledger.pending() or ledger.get("halted")):
            raise RuntimeError("Continuous operation requires no unresolved intent and no halt")

    def _load_experiment(self):
        from .actions import StonkflyActions
        from .data import verify
        from .market import CoinbaseMarket, FixtureMarket
        from .neural.brain import restore_verified
        from .neural.controller import FlyController
        from .okx_market import OKXMarket

        verified = verify()
        args, settings, ledger = self.args, self.settings, self.ledger
        market_type = FixtureMarket if args.fixture else OKXMarket if args.okx else CoinbaseMarket
        self.market = market_type(settings.products)
        previous = ledger.get("observation")
        if previous:
            self.market.history = previous["market_history"]
            if args.fixture:
                self.market.tick = previous["fixture_tick"]
        self.controller = FlyController(settings)
        checkpoint = ledger.get("checkpoint")
        if checkpoint:
            restore_verified(self.controller, self.out, checkpoint, ledger=ledger)
        feed = "fixture" if args.fixture else "okx" if args.okx else "coinbase-public"
        provenance = run_state.build_provenance(
            settings, ledger, self.controller.brain, verified, self.broker.mode, feed,
        )
        run_state.record_provenance(self.out, ledger, provenance, args.migrate_protocol)
        self.guard = Guard(settings, ledger, self.stop_file, clock=self.now)
        self.provider = StonkflyActions(self.guard, self.broker)
        self.action = self.provider.get_actions()[0]
        # Market and account reads use different clients in demo mode.
        if args.okx:
            self.read_clients.append(self.market.client)
        if args.okx_demo:
            self.read_clients.append(self.broker.client)

    def _run_loop(self):
        committed = 0
        while not self.args.steps or committed < self.args.steps:
            started = time.monotonic()
            result = self._tick(started)
            if result is TickResult.STOP:
                break
            if result is TickResult.COMMITTED:
                committed += 1
                if committed % _CLOCK_RECHECK_TICKS == 0:
                    self._refresh_clock()
            if not self.args.fast and (not self.args.steps or committed < self.args.steps):
                sleep_until(started + self.settings.interval_seconds, self.stop_file)

    def _read_retries(self):
        return sum(client.read_retries_used for client in self.read_clients)

    def _tick(self, started):
        ledger, settings = self.ledger, self.settings
        halted = ledger.get("halted")
        if self.stop_file.exists() or halted:
            reason = "STOP file exists" if self.stop_file.exists() else f"run is halted: {halted}"
            print(f"Run stopped before tick: {reason}", file=sys.stderr, flush=True)
            return TickResult.STOP
        run_state.rotate_events(self.out, settings)
        self.broker.reconcile()  # Never covered by the observation retry policy.
        read_started = time.monotonic()
        component = "balance/risk sweep"
        try:
            with observation_budget(self.read_clients, settings.tick_budget_seconds):
                self.broker.verify_balances()
                if ledger.attempts_remaining() == 0:
                    print(
                        "Order attempt budget exhausted; no further submissions. "
                        "Submitted orders were reconciled and run state is preserved.",
                        file=sys.stderr, flush=True,
                    )
                    return TickResult.STOP
                component = "market data"
                quotes = self.market.snapshot()
        except Exception as error:
            if not is_transient_read_failure(error):
                raise
            self._record_read_failure(error, component, read_started)
            return TickResult.WAIT
        readonly_ms = round((time.monotonic() - read_started) * 1000)
        try:
            self.guard.check(quotes, self.now())
        except Veto:
            if not self.stop_file.exists():
                raise
            print("Run stopped before tick: STOP file exists", file=sys.stderr, flush=True)
            return TickResult.STOP
        self.market.record(quotes)
        product = settings.products[ledger.get("tick") % len(settings.products)]
        if self._warmup(product):
            return TickResult.WAIT
        frame, row = self._observe(product, quotes)
        self.consecutive_read_failures = 0
        row["execution"] = self._execute(product, row["neural"]["side"], quotes[product])
        row.update(
            wall_time=time.time(), readonly_duration_ms=readonly_ms,
            read_retries=self._read_retries(),
            tick_duration_ms=round((time.monotonic() - started) * 1000),
        )
        self._publish(frame, row)
        return TickResult.COMMITTED

    def _record_read_failure(self, error, component, started):
        self.consecutive_read_failures += 1
        row = {
            "type": "tick_skipped", "tick": self.ledger.get("tick"), "wall_time": time.time(),
            "component": component, "consecutive": self.consecutive_read_failures,
            "error": type(error).__name__,
            "readonly_duration_ms": round((time.monotonic() - started) * 1000),
            "deadline_exhausted": bool(getattr(error, "deadline", False)),
            "read_retries": self._read_retries(),
        }
        run_state.append_event(self.out, row)
        run_state.emit(row)
        if self.consecutive_read_failures >= self.settings.read_fail_halt_after:
            reason = (
                f"Read-only data unavailable for {self.consecutive_read_failures} "
                f"consecutive ticks (last component: {component})"
            )
            self.ledger.halt(reason)
            raise RuntimeError(reason) from error

    def _warmup(self, product):
        candles = len(self.market.history[product])
        if needs_warmup(candles, self.settings):
            progress = {"candles": candles, "required": self.settings.warmup_candles}
            self.ledger.put("warmup", progress)
            run_state.append_event(self.out, {
                "type": "warming_up", "tick": self.ledger.get("tick"),
                "wall_time": time.time(), **progress,
            })
            run_state.emit({"warming_up": True, **progress})
            return True
        if self.ledger.get("warmup"):
            self.ledger.put("warmup", None)
        return False

    def _observe(self, product, quotes):
        from .display import market_frame
        from .reinforcement import reinforcement

        ledger, settings = self.ledger, self.settings
        quote = quotes[product]
        equity = ledger.equity(quotes)
        kind, delta = reinforcement(equity, ledger.get("anchor"), settings.reward_deadband)
        previous = ledger.get("observation") or {}
        equity_history = [float(value) for value in (previous.get("equity_history") or [])][-119:]
        state = None
        if settings.show_portfolio_state:
            state = {
                "equity_history": equity_history,
                "cash_ratio": float(ledger.cash / equity) if equity else 0.0,
            }
        frame = market_frame(product, self.market.history[product], quote.bid, quote.ask, state=state)
        neural = self.controller.observe(frame, kind)
        label = settings.quote_ccy.lower()
        observation = {
            "neural": neural, "product": product, "quote": quote.json(),
            f"pnl_delta_{label}": str(delta), "market_history": self.market.history,
            "fixture_tick": getattr(self.market, "tick", None),
            "equity_history": equity_history + [float(equity)],
        }
        rebase = settings.reward_anchor == "tick" or ledger.get("tick") % settings.reward_horizon_ticks == 0
        run_state.commit_observation(
            self.out, ledger, self.controller, equity if rebase else None, observation,
        )
        return frame, {
            "tick": ledger.get("tick"), "product": product, "mode": self.broker.mode,
            "quote": quote.json(), f"equity_{label}": str(equity),
            f"pnl_delta_{label}": str(delta), "neural": neural,
        }

    def _execute(self, product, side, observed):
        if side == "HOLD":
            return {"status": "HOLD"}
        try:
            fresh = self.market.snapshot()
        except Exception as error:
            if not is_transient_read_failure(error):
                raise
            return {"status": "VETO", "reason": (
                f"transient market-data failure before execution ({type(error).__name__}); no order attempted"
            )}
        try:
            if abs(fresh[product].bid - observed.bid) / observed.bid > D(self.settings.slippage):
                raise Veto("Price moved beyond neural observation tolerance")
            self.provider.quotes = fresh
            order = self.action.invoke({"product": product, "side": side})
        except Veto as error:
            return {"status": "VETO", "reason": str(error)}
        if (
            self.settings.reward_anchor == "trade"
            and isinstance(order.get("client_order_id"), str)
            and self.ledger.filled_trade(order["client_order_id"])
        ):
            # Only actual fills rebase; a zero-fill FOK cancellation is not a trade.
            self.ledger.put("anchor", str(self.ledger.equity(fresh)))
        return order

    def _publish(self, frame, row):
        from PIL import Image

        run_state.append_event(self.out, row)
        Image.fromarray(frame).save(self.out / "latest-input.png")
        run_state.write_json(self.out / "latest.json", row)
        run_state.emit({
            "tick": row["tick"], "side": row["neural"]["side"],
            "execution": row["execution"]["status"],
            "equity": row[f"equity_{self.settings.quote_ccy.lower()}"],
            "stimulus": row["neural"]["stimulus"],
            "plastic_edges_changed": row["neural"]["memory"]["changed_edges"],
        })
