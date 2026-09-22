"""Run artifacts, protocol provenance and sanitized failure reports."""

import dataclasses
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path


def emit(row):
    print(json.dumps(row, allow_nan=False), flush=True)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def append_event(out, row):
    with (out / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def rotate_events(out, settings, warn=print):
    """Readers can briefly block rotation on Windows; keep the event and report it."""
    path = out / "events.jsonl"
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < settings.events_rotate_mb * 1024 * 1024:
        return
    try:
        for generation in range(settings.events_keep - 1, 0, -1):
            src = out / f"events.{generation}.jsonl"
            if src.exists():
                src.replace(out / f"events.{generation + 1}.jsonl")
        path.replace(out / "events.1.jsonl")
        emit({"events_rotated": True, "bytes": size})
    except OSError as error:
        warn(f"events rotation failed (will retry next tick): {type(error).__name__}")


def attempt_budget_record(ledger):
    # Mutable counters must not make an unchanged protocol fail on restart.
    return {"limit": ledger.get("attempt_limit")}


def build_provenance(settings, ledger, brain, dataset, mode, feed):
    package = Path(__file__).parent
    return {
        "settings": dataclasses.asdict(settings),
        "dataset": dataset,
        "circuit": brain.circuit["report"],
        "vision": brain.visual_report,
        "mode": mode,
        "feed": feed,
        "order_attempt_budget": attempt_budget_record(ledger),
        "decoder": "DNp20 mean R-L: buy/sell; DNpe017 spike gate; otherwise hold. Engineered fixed mapping.",
        "learning_validated": False,
        "pain_receptors_modeled": False,
        "timing": "Each observation advances configured neural_ms regardless of wall-market time; no claim of real-time fly physiology.",
        "source_sha256": {
            str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in package.rglob("*")
            if path.suffix in (".py", ".cpp")
        },
    }


def _signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def record_provenance(out, ledger, provenance, migrate):
    signature = _signature(provenance)
    if ledger.get("provenance_sha256") not in (None, signature):
        if not migrate:
            try:
                previous = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
            # The ledger mismatch remains fatal even if the diagnostic copy is missing.
            diff = {}
            for key in sorted(set(provenance) | set(previous)):
                old, new = _signature(previous.get(key)), _signature(provenance.get(key))
                if old != new:
                    diff[key] = f"{old[:12]} -> {new[:12]}"
            raise RuntimeError(
                f"Run source/protocol changed; components: {diff}. Review the change, "
                "then use a separate run directory or pass --migrate-protocol to "
                "adopt it in place (money state, budget and attempts are kept)."
            )
        previous = ledger.migrate_protocol(signature, note="adopted by --migrate-protocol")
        emit({"protocol_migrated": True, "from": (previous or "")[:12], "to": signature[:12]})
    else:
        ledger.put("provenance_sha256", signature)
    write_json(out / "provenance.json", provenance)


def commit_observation(out, ledger, controller, anchor, observation):
    """Persist the brain, then commit its hash and observation before any order."""
    path = out / f"brain-{ledger.get('tick') % 2}.npz"
    controller.save(path)
    previous = ledger.get("checkpoint") or {}
    ledger.commit_tick(anchor, {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "prev_file": previous.get("file"),
        "prev_sha256": previous.get("sha256"),
    }, observation)


def _diagnostic(error, reason=None):
    frames = traceback.extract_tb(error.__traceback__)
    origin = frames[-1] if frames else None
    internal = origin and Path(origin.filename).is_relative_to(Path(__file__).parent)
    return {
        "type": type(error).__name__,
        "reason": reason if reason is not None else (
            str(error) if internal else
            "External dependency error; review connection and account state."
        ),
        "locations": [f"{Path(frame.filename).name}:{frame.lineno} {frame.name}" for frame in frames],
    }


def preflight_read_failure(out, error):
    write_json(out / "error.json", _diagnostic(error, reason=(
        "Transient read-only failure during preflight; execution not started "
        "and not halted; the next launch retries."
    )))
    print(
        "Preflight hit a transient read-only failure; nothing was started "
        "and the ledger was not halted. The next launch will retry.",
        file=sys.stderr, flush=True,
    )
    raise SystemExit(1) from None


def report_failure(out, ledger, error):
    # An unused directory should not acquire a persistent halt on startup failure.
    started = ledger.get("tick") or ledger.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    if started and not ledger.get("halted"):
        ledger.halt(type(error).__name__)
    diagnostic = _diagnostic(error)
    unverified = list(getattr(error, "unverified", ()) or ())
    if unverified:
        diagnostic["algo_coverage_unverified"] = [
            {"ordType": kind, "okx_code": code} for kind, code in unverified
        ]
    if getattr(error, "okx_code", None):
        diagnostic["okx_code"] = error.okx_code
    write_json(out / "error.json", diagnostic)
    print(
        f"Stopped safely: {type(error).__name__}. Inspect local state and reconcile before restarting.",
        file=sys.stderr,
    )
    if unverified:
        print(
            "Untriggered algo order coverage unavailable for: "
            + ",".join(f"{kind}(code={code})" for kind, code in unverified)
            + ". No documented substitute exists; the run stays stopped.",
            file=sys.stderr,
        )
