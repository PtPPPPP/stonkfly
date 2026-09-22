"""Offline pretraining: let the fly explore historical candles. Never trades.

What this tool is: the same fly (full MaleCNS connectome, same dopamine-modulated
plasticity rule, same fixed decoder, same reinforcement signal, same guard sizing)
run over a replayed past instead of the live present, at wall-clock speeds the
CPU allows. It produces a checkpoint that can later be deployed as a clearly
labeled experiment.

What this tool is NOT: a way to teach the fly a strategy. No labels, no weight
surgery, no external policy -- knowledge can only grow from the fly's own
experience through the existing plasticity rule. And whatever the outcome, an
offline exploration is never "validated profitable learning": historical windows
overfit, regimes change, and the honest claim stays "engineered reinforcement +
local plasticity; no validated profitable learning".

    .venv\\Scripts\\python.exe tools\\fly_pretrain.py --ticks 500
    .venv\\Scripts\\python.exe tools\\fly_pretrain.py --status

Data: public OKX candles (no credentials), fetched once and cached under the
run directory. State (tick, virtual clock, checkpoint, paper accounting) lives
in a paper-mode ledger, so any run can be interrupted and resumed.

Deployment is a separate, explicit decision (stop the worker first; the tool
refuses while it holds the run lock):
    .venv\\Scripts\\python.exe tools\\fly_pretrain.py --deploy-to runs\\okx-demo-v2
"""

import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stonkfly.broker import PaperBroker  # noqa: E402
from stonkfly.config import Settings  # noqa: E402
from stonkfly.display import market_frame  # noqa: E402
from stonkfly.neural.controller import FlyController  # noqa: E402
from stonkfly.ledger import Ledger  # noqa: E402
from stonkfly.market import Quote  # noqa: E402
from stonkfly.reinforcement import reinforcement  # noqa: E402
from stonkfly.risk import Guard, Veto  # noqa: E402
from stonkfly.config import D  # noqa: E402

# One live tick is one observed minute; the virtual clock advances the same 60
# virtual seconds per tick, so the cooldown and the daily cap shape order
# density exactly as they do live while the wall clock runs flat out.
VIRTUAL_SECONDS_PER_TICK = 60
# History window of the rendered chart, matching OKXMarket: 120 completed 1m
# closes, and the current bar enters the window on the NEXT tick, not this one.
WINDOW = 120


def build_client():
    """Public, credential-free client. Environment proxies are honored."""
    from stonkfly.okx_client import OKXClient

    return OKXClient(api_key=None, secret=None, passphrase=None, timeout=30)


def fetch_candles(client, inst_id, bar, bars):
    """The last ``bars`` completed candles, oldest first.

    Walks back through the recent-candles endpoint (limited to roughly the
    last 24h of 1m bars) and then the history endpoint, paginating with the
    documented ``after`` cursor until the count is reached or the exchange
    stops returning rows. Returns ``(closes, complete)`` -- a truncated scan
    is reported, never silently used as if it were the requested depth.
    """
    rows = {}
    ts = None
    endpoint = "/api/v5/market/candles"
    limit = "300"
    max_pages = max(220, bars // 40)
    for _ in range(max_pages):
        params = {"instId": inst_id, "bar": bar, "limit": limit}
        if ts is not None:
            params["after"] = str(ts)
        # The client's read path: bounded retries over transport failures and
        # OKX's transient codes (429/5xx/timeouts), identical to the live run.
        d = client._get(endpoint, params=params, what="candles page")
        data = d.get("data")
        if not isinstance(data, list) or not data:
            if endpoint == "/api/v5/market/candles":
                # the recent window is exhausted; continue into the archive
                endpoint = "/api/v5/market/history-candles"
                limit = "100"
                continue
            break
        for row in data:
            if len(row) >= 9 and row[8] == "1":  # completed candles only
                rows[int(row[0])] = float(row[4])
        oldest = min(int(r[0]) for r in data)
        if ts is not None and oldest >= ts:
            break  # no progress
        ts = oldest
        if len(rows) >= bars:
            break
    closes = [rows[ts_] for ts_ in sorted(rows)][-bars:]
    return closes, len(closes) >= bars


def _write_json_atomic(path, payload):
    """Write JSON via tmp + fsync + atomic replace, so a crash can never leave
    a truncated file that every future resume would choke on."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_data(client, out, product, bar, bars, refresh):
    """Fetch once, cache to disk atomically, reuse deterministically on resume.

    Once a cache exists it IS the dataset for this directory: resume indexes
    into exactly the array the earlier ticks trained on. An existing ledger
    therefore forbids replacement of its dataset, including ``--refresh``.
    A corrupt cache fails explicitly; restore the original data or start in
    a new directory. ``bars`` sizes the initial fetch only.
    """
    cache = out / "candles.json"
    has_ledger = (out / "ledger.sqlite").exists()
    if has_ledger and (refresh or not cache.exists()):
        raise RuntimeError(
            "Cannot replace candles for an existing ledger; restore its original "
            "cache or use a new run directory"
        )
    if cache.exists() and not refresh:
        try:
            blob = json.loads(cache.read_text())
        except (ValueError, OSError) as e:
            raise RuntimeError(
                "Cannot read candle cache; restore the original data or use a new run directory"
            ) from e
        if isinstance(blob, dict) and blob.get("closes"):
            if blob.get("product") == product and blob.get("bar") == bar:
                if len(blob["closes"]) < WINDOW + 1:
                    raise RuntimeError("Candle cache has insufficient history")
                return blob["closes"], blob.get("complete", True), True
        raise RuntimeError("Candle cache is invalid or belongs to another product/bar")
    inst = client.instruments(product)
    if inst is None or inst.get("instId") != product:
        raise RuntimeError(f"Instrument {product} unavailable")
    closes, complete = fetch_candles(client, product, bar, bars)
    if len(closes) < WINDOW + 1:
        raise RuntimeError(
            f"only {len(closes)} completed candles available; need {WINDOW + 1}"
        )
    _write_json_atomic(cache, {
        "product": product,
        "bar": bar,
        "complete": complete,
        "increments": {
            "lotSz": inst["lotSz"], "tickSz": inst["tickSz"], "minSz": inst["minSz"],
        },
        "closes": closes,
    })
    return closes[:bars], complete, False


def pretrain_settings(product, reward_anchor="tick", horizon=1, show_state=False):
    """The live protocol with documented offline overrides.

    paper_fee matches the demo environment's actual 0.1% taker fee (the live
    okx-demo ledger never uses paper_fee, so this changes nothing there);
    daily_orders keeps the live value of 24, which under the virtual clock
    reproduces the live order density of roughly one order per hour. The
    reward-shaping and portfolio-state arguments mirror the deployment flags so
    a pretrained brain trains in-distribution with the run it will join.
    """
    return Settings(
        products=(product,),
        paper_fee="0.001",
        reward_anchor=reward_anchor,
        reward_horizon_ticks=horizon,
        show_portfolio_state=show_state,
    )


def reconcile_virtual_intents(ledger):
    """Paper-mode startup recovery for intents left by a crashed run.

    A PREPARED intent can be proven never-executed in a virtual run: paper
    requests cannot leave the process, and ``begin_attempt`` (the transition
    that would make it UNKNOWN) had not run yet. It is closed REJECTED with an
    append-only audit record. An UNKNOWN intent is NOT auto-closed -- execution
    may have already settled money state before the crash -- so the run refuses
    to continue until an operator resolves it; this raises, and ``main``
    propagates a non-zero exit code.
    """
    for row in ledger.pending():
        cid = row["id"]
        if row["status"] == "PREPARED":
            trail = ledger.get("resolved_orders") or []
            trail.append(
                {
                    "id": cid,
                    "reason": "virtual_intent_never_executed",
                    "source": "pretrain_recovery",
                    "at": time.time(),
                    "created": row["created"],
                    "note": "paper PREPARED cannot have been submitted; closed at startup",
                }
            )
            with ledger.transaction():
                ledger.put("resolved_orders", trail)
                ledger.reject_prepared(cid)
            print(f"recovered: closed never-executed virtual intent {cid[:8]}…", flush=True)
        else:
            raise RuntimeError(
                f"Virtual run crashed with {row['status']} intent {cid}; it may already "
                "have settled money state, so it is left untouched. Resolve it "
                "manually (or delete the run directory to start over) before resuming."
            )


def open_run(out, settings, adopt_settings=False):
    """The ledger and, when resuming, the controller restored from checkpoint."""
    ledger = Ledger(out / "ledger.sqlite", settings, "paper",
                    adopt_settings=adopt_settings)
    try:
        reconcile_virtual_intents(ledger)
        controller = FlyController(settings)
        cp = ledger.get("checkpoint")
        if cp:
            from stonkfly.neural.brain import restore_verified

            restore_verified(controller, out, cp, ledger=ledger)
    except BaseException:
        ledger.close()
        raise
    return ledger, controller


def before_submit(ledger, plan):
    """The pretrain-local send gate.

    guard.before_submit adds a quote-age check against the wall clock, which is
    meaningless under the virtual clock. The invariant it protects locally is
    intent ownership, re-checked here.
    """
    pending = ledger.pending()
    if (
        len(pending) != 1
        or pending[0]["id"] != plan["client_order_id"]
        or pending[0]["status"] != "PREPARED"
        or pending[0]["plan"] != plan
    ):
        raise Veto("Intent ownership mismatch")


def run_pretrain(out, settings, closes, ticks, product, adopt_settings=False):
    ledger, fly = open_run(out, settings, adopt_settings=adopt_settings)
    with contextlib.closing(ledger):
        return _replay(out, settings, closes, ticks, product, ledger, fly)


def _replay(out, settings, closes, ticks, product, ledger, fly):
    from stonkfly.run_state import commit_observation

    guard = Guard(settings, ledger, out / "STOP")
    paper = PaperBroker(settings, ledger)
    increments = json.loads((out / "candles.json").read_text())["increments"]

    observation = ledger.get("observation") or {}
    cursor = int(observation.get("candle_cursor", WINDOW))
    # The observation records the last processed candle, while cursor points
    # to the next one. Resume must advance both by one virtual minute.
    virtual_now = (
        float(observation["virtual_now"]) + VIRTUAL_SECONDS_PER_TICK
        if observation else time.time()
    )
    done = 0
    started = time.monotonic()
    vetoes = 0
    failure = None

    try:
        while cursor < len(closes) and (ticks == 0 or done < ticks):
            close = closes[cursor]
            window = closes[max(0, cursor - WINDOW):cursor]
            quote = Quote(
                product, D(close), D(close), virtual_now,
                D(increments["lotSz"]), None, D(increments["tickSz"]), None,
                D(increments["minSz"]),
            )
            quotes = {product: quote}
            try:
                guard.check(quotes, virtual_now)
            except Veto as e:
                # The loss stop (or any other guard refusal) halts the pretrain
                # ledger exactly as it halts a live run; end the run cleanly and
                # let the report carry the reason. An unresolved intent here
                # means the run cannot continue honestly: it is reported as a
                # failure via the exit code below.
                print(f"pretrain stopped by the guard: {e}", flush=True)
                break
            equity = ledger.equity(quotes)
            anchor = ledger.get("anchor")
            kind, delta = reinforcement(equity, anchor,
                                        settings.reward_deadband)
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
            frame = market_frame(product, window, close, close, state=state)
            neural = fly.observe(frame, kind)
            rebase = (
                settings.reward_anchor == "tick"
                or ledger.get("tick") % settings.reward_horizon_ticks == 0
            )
            commit_observation(
                out, ledger, fly,
                equity if rebase else None,
                {
                    "neural": neural,
                    "product": product,
                    "quote": quote.json(),
                    "pnl_delta_usdt": str(delta),
                    "market_history": {product: list(window)},
                    "equity_history": equity_history,
                    "candle_cursor": cursor + 1,
                    "virtual_now": virtual_now,
                },
            )
            cursor += 1
            if neural["side"] != "HOLD":
                try:
                    plan = guard.plan(product, neural["side"], quotes, now=virtual_now)
                    plan = ledger.reserve(plan, virtual_now)
                    paper.execute(plan, lambda p: before_submit(ledger, p))
                    if settings.reward_anchor == "trade" and ledger.filled_trade(
                        plan["client_order_id"]
                    ):
                        # Trade-anchored reinforcement: grade the following
                        # ticks against the post-trade equity. The semantic
                        # test is the settled base fill, so only an actual
                        # trade re-anchors.
                        ledger.put("anchor", str(ledger.equity(quotes)))
                except Veto:
                    vetoes += 1
            virtual_now += VIRTUAL_SECONDS_PER_TICK
            done += 1
            if done % 25 == 0:
                rate = done / (time.monotonic() - started)
                print(f"tick {ledger.get('tick')} cursor {cursor}/{len(closes)} "
                      f"cash {ledger.get('cash')} {rate:.2f} ticks/s", flush=True)
    except Exception as e:
        # An unexpected exception must never masquerade as a completed run:
        # record it and let the exit code fail (the report is written below).
        failure = e
        print(f"pretrain failed: {type(e).__name__}: {e}", flush=True)
    finally:
        wall = time.monotonic() - started
        ended_unresolved = bool(ledger.get("halted")) or bool(ledger.pending())
        report = {
            "product": product,
            "closes_available": len(closes),
            "cursor": cursor,
            "ledger_tick": ledger.get("tick"),
            "ticks_this_run": done,
            "wall_seconds": round(wall, 1),
            "ticks_per_second": round(done / wall, 3) if wall else None,
            "order_vetoes": vetoes,
            "cash": ledger.get("cash"),
            "positions": ledger.get("positions"),
            "checkpoint": ledger.get("checkpoint"),
            "halted": ledger.get("halted"),
            "failure": None if failure is None else type(failure).__name__,
            "claim": "offline exploration; NOT validated profitable learning",
        }
        _write_json_atomic(out / "report.json", report)
        print(json.dumps({k: report[k] for k in (
            "ledger_tick", "ticks_this_run", "ticks_per_second", "cash",
            "positions", "halted", "failure")}), flush=True)
    # Exit code contract: 0 only for a clean end (data exhausted, or the
    # requested tick count reached) with a fully resolved ledger. A guard
    # halt, an unresolved intent or an unexpected exception fails the run so
    # unattended supervision can tell success from silent stop.
    if failure is not None or ended_unresolved:
        return 1
    return 0


def deploy(source, target):
    """Copy the pretrained checkpoint into a run directory, checked and audited.

    Refuses while the target's worker lock is held, or while the target has an
    unresolved intent, a halt, or an unconfirmed migration. Records the
    deployment in an append-only trail. The live brain is replaced, not merged;
    the trail names exactly what was replaced, by what, from where.
    """
    from stonkfly import locking

    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or not source.is_dir() or not target.is_dir():
        print("refusing: source and target must be distinct existing run directories")
        return 1
    with contextlib.ExitStack() as stack:
        for directory in sorted((source, target)):
            try:
                lock = locking.acquire(directory / "worker.lock")
            except BlockingIOError:
                print("refusing: a worker owns a deployment run directory (stop it first)")
                return 1
            stack.callback(locking.release, lock)
        return _deploy_locked(source, target)


def _deploy_locked(source, target):
    from stonkfly import audit
    from stonkfly.neural.brain import checkpoint_path

    view = audit.read_ledger(target)
    if audit.identity_of(view).get("environment") != "okx-demo":
        print("refusing: the target is not an okx-demo run directory")
        return 1
    if audit.pending(view):
        print("refusing: the target has an unresolved intent")
        return 1
    if view["meta"].get("halted"):
        print("refusing: the target is halted")
        return 1
    if (view["meta"].get("migration") or {}).get("state") == "staged":
        print("refusing: the target holds an unconfirmed migration")
        return 1
    source_view = audit.read_ledger(source)
    cp = source_view["meta"].get("checkpoint")
    if not isinstance(cp, dict) or not cp.get("file"):
        print("refusing: the source records no checkpoint")
        return 1
    source_file = checkpoint_path(source, cp["file"])
    checkpoint_bytes = source_file.read_bytes()
    if hashlib.sha256(checkpoint_bytes).hexdigest() != cp["sha256"]:
        print("refusing: the source checkpoint does not match its recorded hash")
        return 1
    # Read the target's recorded configuration while its lock is held. Older
    # default-protocol runs have no provenance file; Ledger still verifies the
    # exact signature before allowing any mutation.
    provenance = target / "provenance.json"
    settings = Settings(products=("BTC-USDT",))
    if provenance.exists():
        values = json.loads(provenance.read_text())["settings"]
        values["products"] = tuple(values["products"])
        settings = Settings(**values)
    ledger = Ledger(target / "ledger.sqlite", settings, "okx-demo",
                    identity=view["meta"]["identity"])
    try:
        # A new deployment never overwrites a file referenced by the ledger.
        # Persist the immutable artifact before committing its new reference.
        name = f"brain-pretrained-{cp['sha256']}.npz"
        dest = target / name
        temporary = dest.with_suffix(".partial")
        with temporary.open("wb") as handle:
            handle.write(checkpoint_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(dest)
        from stonkfly.neural.brain import _fsync_directory

        _fsync_directory(target)
        with ledger.transaction():
            trail = ledger.get("checkpoint_deployments") or []
            trail.append({
                "at": time.time(),
                "file": name,
                "sha256": cp["sha256"],
                "source": str(source),
                "source_tick": source_view["meta"].get("tick"),
                "replaced": ledger.get("checkpoint"),
                "note": "offline exploration; NOT validated profitable learning",
            })
            ledger.put("checkpoint_deployments", trail)
            ledger.put("checkpoint", {"file": name, "sha256": cp["sha256"]})
        print(f"deployed {name} from {source} (recorded in checkpoint_deployments)")
        print("the next worker start restores the pretrained brain")
        return 0
    finally:
        ledger.close()


def show_status(out):
    """The end-of-run report, or -- while a run is still going -- the live
    state, read from the ledger read-only (safe next to a running worker)."""
    report = out / "report.json"
    if report.exists():
        print(report.read_text(), end="")
        return 0
    from stonkfly import audit

    try:
        view = audit.read_ledger(out)
    except FileNotFoundError:
        print(f"no pretraining run under {out}")
        return 1
    meta = view["meta"]
    obs = meta.get("observation") or {}
    print(json.dumps({
        "status": "running or interrupted (final report appears when a run ends)",
        "ledger_tick": meta.get("tick"),
        "candle_cursor": obs.get("candle_cursor"),
        "cash": meta.get("cash"),
        "positions": meta.get("positions"),
        "halted": meta.get("halted"),
        "equity_history_points": len(obs.get("equity_history") or []),
        "checkpoint": meta.get("checkpoint"),
    }, indent=2))
    return 0


def lower_process_priority():
    """Batch courtesy: the live worker keeps scheduling priority.

    The pretrain competes for the same CPU as the trading worker; without
    this, its multi-second neural integrations can stretch the live worker's
    tick past the quote-age window and its trades would be vetoed.
    """
    if os.name == "nt":
        import ctypes

        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000  # BELOW_NORMAL
        )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fly_pretrain")
    parser.add_argument("--product", default="BTC-USDT")
    parser.add_argument("--bar", default="1m")
    parser.add_argument("--bars", type=int, default=3000,
                        help="candles to fetch; one tick consumes one candle")
    parser.add_argument("--ticks", type=int, default=0,
                        help="ticks this run; 0 = until the data is exhausted")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--refresh", action="store_true",
                        help="refetch candles even if cached")
    parser.add_argument("--reward-anchor", choices=["tick", "trade"],
                        default="tick")
    parser.add_argument("--reward-horizon-ticks", type=int, default=1)
    parser.add_argument("--show-portfolio-state", action="store_true")
    parser.add_argument("--status", action="store_true",
                        help="print the latest pretraining report")
    parser.add_argument(
        "--adopt-settings",
        action="store_true",
        help="adopt a changed Settings signature on resume (recorded in the "
        "settings migration trail). Needed once after an upgrade that adds "
        "operational settings; training behavior is unchanged.",
    )
    parser.add_argument("--deploy-to", type=Path, default=None,
                        help="deploy the pretrained checkpoint to a run directory")
    args = parser.parse_args(argv)

    out = args.out or (_ROOT / "runs" / f"pretrain-{args.product}")
    if args.deploy_to:
        return deploy(out, args.deploy_to)
    if args.status:
        return show_status(out)

    out.mkdir(parents=True, exist_ok=True)
    lower_process_priority()
    settings = pretrain_settings(
        args.product, args.reward_anchor, args.reward_horizon_ticks,
        args.show_portfolio_state,
    )
    from stonkfly import locking

    try:
        lock = locking.acquire(out / "worker.lock")
    except BlockingIOError:
        print("pretrain cannot start: a worker owns this run directory", flush=True)
        return 1
    try:
        client = build_client()
        try:
            closes, complete, cached = load_data(client, out, args.product, args.bar,
                                                 max(args.bars, WINDOW + 1), args.refresh)
        except RuntimeError as e:
            print(f"pretrain cannot start: {e}", flush=True)
            return 1
        print(f"candles: {len(closes)} ({'cached' if cached else 'fetched'}"
              f"{'' if complete else ', TRUNCATED'})", flush=True)
        return run_pretrain(out, settings, closes, args.ticks, args.product,
                            adopt_settings=args.adopt_settings)
    finally:
        locking.release(lock)


if __name__ == "__main__":
    sys.exit(main())
