"""Read-only terminal view of a fly run's observations and decisions.

The watcher consumes only files the run loop already writes: it tails
``events.jsonl`` for per-tick neural records and opens ``ledger.sqlite``
read-only for run metadata. It holds no worker lock and writes nothing to
the run directory, so watching cannot influence execution. The panel
describes engineered stimuli and a fixed decoder; it is not evidence of
learning or of profitable behaviour.
"""

import json
import os
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

TIMELINE_TICKS = 56
REPLAY_WINDOW = 300

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[38;5;245m"
GREEN = "\x1b[38;5;114m"
RED = "\x1b[38;5;174m"
BLUE = "\x1b[38;5;75m"
AMBER = "\x1b[38;5;215m"

SIDE_COLOR = {"BUY": GREEN, "SELL": RED, "HOLD": DIM}
STIM_COLOR = {"reward": GREEN, "aversive": RED, "none": DIM}
STIM_CELL = {
    "reward": "PAM11 reward dopamine",
    "aversive": "PPL101 aversive dopamine",
    "none": "no dopamine pulse",
}
DISCLAIMER = (
    "engineered stimuli + fixed decoder; learning and profitable behaviour not validated"
)


def _visible(line):
    import re

    return len(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line))


def _pad(line, width):
    return line + " " * max(0, width - _visible(line))


def _num(value, digits=4):
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _meter(fraction, width=3):
    try:
        frac = max(0.0, min(1.0, float(fraction)))
    except (TypeError, ValueError):
        frac = 0.0
    full = int(frac * width)
    return "▓" * full + "░" * (width - full)


def sparkline(values, width=48):
    tail = [float(v) for v in list(values)[-width:]]
    if not tail:
        return ""
    lo, hi = min(tail), max(tail)
    if hi <= lo:
        hi = lo + 1.0
    return "".join("▁▂▃▄▅▆▇█"[min(7, int((v - lo) / (hi - lo) * 8))] for v in tail)


def area_rows(values, width, height):
    """Multi-row area chart; returns plain-text rows, top first."""
    tail = [float(v) for v in list(values)[-width:]]
    rows = [" " * width for _ in range(height)]
    if len(tail) < 2:
        return rows
    lo, hi = min(tail), max(tail)
    if hi <= lo:
        hi = lo + 1.0
    pad = width - len(tail)
    for i, v in enumerate(tail):
        x = pad + i
        level = (v - lo) / (hi - lo) * height
        for row in range(height):
            from_bottom = height - row
            if level >= from_bottom:
                ch = "█"
            elif level > from_bottom - 1:
                ch = "▄"
            else:
                continue
            rows[row] = rows[row][:x] + ch + rows[row][x + 1:]
    return rows


def parse_event(line):
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    return event if isinstance(event, dict) else None


def read_meta(out):
    """Full parsed ledger meta, or {} when the ledger is missing or unreadable.

    The one read-only helper every observer (status, watch, serve, health)
    builds on, so corrupted state, missing files, partial writes and busy
    databases get identical treatment everywhere: degrade to empty, never
    modify anything, never crash the observer.
    """
    path = Path(out) / "ledger.sqlite"
    if not path.exists():
        return {}
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT key, value FROM meta").fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return {}
    meta = {}
    for key, value in rows:
        try:
            meta[key] = json.loads(value)
        except (TypeError, ValueError):
            meta[key] = None
    return meta


def read_pending_count(out):
    """Unresolved intent count, or None when it cannot be determined."""
    path = Path(out) / "ledger.sqlite"
    if not path.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return db.execute(
                "SELECT COUNT(*) FROM orders WHERE status NOT IN ('SETTLED','REJECTED')"
            ).fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error:
        return None


def read_status(out):
    """Read-only snapshot of run metadata; absent or busy databases yield {}."""
    meta = read_meta(out)
    if not meta:
        return {}
    observation = meta.get("observation")
    status = {
        k: meta.get(k)
        for k in ("mode", "tick", "halted", "cash", "positions", "attempt_limit", "order_attempts")
    }
    # The committed observation carries the full per-product price history
    # that events.jsonl rows do not repeat.
    history = observation.get("market_history") if isinstance(observation, dict) else None
    status["market_history"] = history if isinstance(history, dict) else {}
    return status


# Mirrors the default Settings.read_fail_halt_after: at this many consecutive
# skipped ticks the worker would have halted itself, so the network reads as
# "down" rather than merely "degraded".
_DOWN_AFTER_CONSECUTIVE_SKIPS = 5


def read_health(out):
    """Derived, read-only health snapshot shared by status, serve and tools.

    Pure observation: nothing here writes or decides anything -- it projects
    the ledger meta plus a bounded tail of events.jsonl into one small dict,
    so every consumer shows the same state and none of them drift apart.
    """
    out = Path(out)
    meta = read_meta(out)
    latest = _read_latest(out / "latest.json")
    tail = []
    path = out / "events.jsonl"
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 65536))
            chunk = handle.read()
        tail = [e for e in (parse_event(l) for l in chunk.split(b"\n")) if e][-20:]
    except OSError:
        pass

    last_row = latest or (tail[-1] if tail else {})
    newest = tail[-1] if tail else None
    skip = newest if (newest or {}).get("type") == "tick_skipped" else None
    last_good = next(
        (e for e in reversed(tail) if e.get("wall_time") and "neural" in e), None
    )
    consecutive = (skip or {}).get("consecutive", 0)

    if meta.get("halted"):
        worker = "halted"
    elif meta.get("warmup"):
        worker = "warming_up"
    elif last_row.get("wall_time") and time.time() - last_row["wall_time"] > 300:
        worker = "stopped"
    else:
        worker = "running" if last_row.get("wall_time") else "stopped"

    if skip is not None and (latest is None or last_row is skip):
        network = "down" if consecutive >= _DOWN_AFTER_CONSECUTIVE_SKIPS else "degraded"
    elif last_good is not None or latest is not None:
        network = "ok"
    else:
        network = "unknown"

    offset = meta.get("clock_offset")
    pending = read_pending_count(out)
    if meta.get("halted"):
        ledger_state = "halted"
    elif pending:
        ledger_state = "unresolved"
    elif pending is None and not meta:
        ledger_state = "unknown"
    else:
        ledger_state = "ok"

    def _iso(wall):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall)) if wall else None

    return {
        "worker": worker,
        "network": network,
        "clock": (
            "unknown" if offset is None
            else ("degraded" if abs(offset) > 5.0 else "ok")
        ),
        "ledger": ledger_state,
        "last_tick_utc": _iso((last_good or {}).get("wall_time")),
        "last_successful_read_utc": _iso((last_good or {}).get("wall_time")),
        "consecutive_read_failures": consecutive if skip is not None else 0,
        "tick_duration_ms": last_row.get("tick_duration_ms"),
        "readonly_duration_ms": last_row.get("readonly_duration_ms"),
        "proxy": "unknown",
        "warmup": meta.get("warmup"),
    }


def _equity(event):
    for key, value in event.items():
        if key.startswith("equity_"):
            return key[len("equity_"):].upper(), value
    return "", None


def _rule(add, title, width):
    pad = max(1, width - len(title) - 4)
    add(f"{DIM}├─ {title} " + "─" * pad + RESET)


def _side_letter(event):
    side = (event.get("neural") or {}).get("side")
    return {"BUY": "B", "SELL": "S", "HOLD": "·"}.get(side, "?")


def _stim_letter(event):
    stim = (event.get("neural") or {}).get("stimulus")
    return {"reward": "R", "aversive": "A", "none": "-"}.get(stim, "?")


def _waiting_frame(label, status, width=100):
    lines = [f"{BOLD}STONKFLY WATCH{RESET} {DIM}· {label}{RESET}"]
    lines.append(f"{DIM}{'─' * max(1, width)}{RESET}")
    lines.append("")
    lines.append(f"{AMBER}WAITING FOR EVENTS{RESET}")
    lines.append("")
    lines.append("No ticks are recorded in this run directory yet. Start a run in")
    lines.append("another terminal, for example:")
    lines.append("")
    lines.append(f"  {BLUE}python -m stonkfly run --fixture --out {label}{RESET}")
    lines.append("")
    lines.append("Or replay a recorded run with:")
    lines.append("")
    lines.append(f"  {BLUE}python -m stonkfly watch --out {label} --replay{RESET}")
    lines.append("")
    if status.get("halted"):
        lines.append(f"{RED}run is halted: {status['halted']}{RESET}")
    lines.append(f"{DIM}read-only observer · {DISCLAIMER}{RESET}")
    return "\n".join(line + "\x1b[K" for line in lines)


def _fly(eye_color, chest_color, belly_color):
    """Top-view fly: eyes = latest price move, chest = decision, belly = P&L."""
    return [
        f"{DIM}  \\  ‿  /{RESET}",
        f"{DIM}   \\ ‿ /{RESET}",
        f"{DIM}╭─{RESET}{eye_color}██{RESET}{DIM}─{RESET}{eye_color}██{RESET}{DIM}─╮{RESET}",
        f"{DIM}│{RESET} {chest_color}████{RESET} {DIM}│{RESET}",
        f"{DIM}─┤{RESET} {chest_color}████{RESET} {DIM}├─{RESET}",
        f"{DIM}│{RESET} {belly_color}▄██▄{RESET} {DIM}│{RESET}",
        f"{DIM} ╰─────╯{RESET}",
        f"{DIM}   ╹ ╹{RESET}",
    ]


def _chart_panel(prices, product, quote, width=50, height=6):
    up = len(prices) < 2 or float(prices[-1]) >= float(prices[0])
    color = GREEN if up else RED
    rows = [f"{BOLD}{product}{RESET}  {_num(quote.get('bid'), 2)}  {DIM}last {len(prices)} pts{RESET}"]
    body = area_rows(prices, width, height)
    rows.extend(f"{color}{row}{RESET}" for row in body)
    return rows


def _side_by_side(left_rows, right_rows, left_width=13):
    out = []
    for i in range(max(len(left_rows), len(right_rows))):
        l = left_rows[i] if i < len(left_rows) else ""
        r = right_rows[i] if i < len(right_rows) else ""
        out.append(_pad(l, left_width) + r)
    return out


def _scale_bar(diff, reference, width=45):
    frac = max(-1.0, min(1.0, diff / reference)) if reference > 0 else 0.0
    pos = int(round((frac + 1) / 2 * (width - 1)))
    pos = max(1, min(width - 2, pos))
    rail = "─" * width
    rail = rail[:pos] + "●" + rail[pos + 1:]
    color = GREEN if diff > 0 else RED if diff < 0 else DIM
    return f"{DIM}SELL ◄{RESET}{color}{rail}{RESET}{DIM}► BUY{RESET}"


def _histogram(history, width=44, height=3):
    recent = list(history)[-width:]
    diffs = [
        abs(float((e.get("neural") or {}).get("difference_hz") or 0)) for e in recent
    ]
    peak = max(diffs + [1e-9])
    rows = []
    for row in range(height):
        cells = []
        for e, d in zip(recent, diffs):
            n = e.get("neural") or {}
            color = SIDE_COLOR.get(n.get("side"), DIM)
            level = d / peak * height
            from_bottom = height - row
            if level >= from_bottom:
                ch = "█"
            elif level > from_bottom - 1:
                ch = "▄"
            else:
                cells.append(" ")
                continue
            cells.append(f"{color}{ch}{RESET}")
        rows.append("".join(cells))
    return rows


def render_frame(current, history, status, *, label="", replay=False, width=100):
    if current is None:
        return _waiting_frame(label, status, width)
    neural = current.get("neural") or {}
    memory = neural.get("memory") or {}
    execution = current.get("execution") or {}
    product = current.get("product", "?")
    quote = current.get("quote") or {}
    prices = (status.get("market_history") or {}).get(product)
    if not isinstance(prices, list) or not prices:
        prices = [
            float(e["quote"]["bid"])
            for e in history
            if e.get("product") == product
            and (e.get("quote") or {}).get("bid") is not None
        ]
    quote_ccy, equity = _equity(current)
    delta = current.get(f"pnl_delta_{quote_ccy.lower()}")
    mode = status.get("mode") or current.get("mode", "?")
    tag = "REPLAY" if replay else "LIVE"
    side = neural.get("side", "?")
    stim = neural.get("stimulus", "none")

    # --- fly + market chart side by side ---------------------------------
    if len(prices) >= 2:
        eye_color = GREEN if float(prices[-1]) >= float(prices[-2]) else RED
    else:
        eye_color = BLUE
    chest_color = SIDE_COLOR.get(side, DIM)
    eq_values = [
        float(e.get(f"equity_{quote_ccy.lower()}"))
        for e in history
        if e.get(f"equity_{quote_ccy.lower()}") is not None
    ]
    if len(eq_values) >= 2:
        belly_color = GREEN if eq_values[-1] >= eq_values[0] else RED
    else:
        belly_color = BLUE
    left = _fly(eye_color, chest_color, belly_color)
    right = _chart_panel(prices, product, quote, width=54, height=8)
    lines = []
    add = lines.append
    add(f"{BOLD}STONKFLY WATCH{RESET} {DIM}· {label} · {tag}{RESET}")
    add(
        f"{DIM}{mode} · {product} · tick {current.get('tick', '?')} · "
        + time.strftime("%H:%M:%S", time.localtime(current.get("wall_time", time.time())))
        + RESET
    )
    add("")
    lines.extend(_side_by_side(left, right))
    eq_line = f"{DIM}equity{RESET} {_num(equity)} {quote_ccy}  {GREEN}{sparkline(eq_values, 24)}{RESET}"
    add(_pad("", 13) + eq_line)
    _rule(add, "SENSORY → BRAIN → ACTION", width)

    # --- signal flow ------------------------------------------------------
    kc_peak = max(
        [float((e.get("neural") or {}).get("KC_spikes") or 0) for e in history]
        + [float(neural.get("KC_spikes") or 0), 1.0]
    )
    kc_fraction = float(neural.get("KC_spikes") or 0) / kc_peak
    plastic = max(1, int(memory.get("plastic_edges") or 1))
    changed = int(memory.get("changed_edges") or 0)
    stim_full = STIM_COLOR.get(stim, DIM)
    add(
        f"eyes {stim_full}{_meter(1.0)}{RESET} ─→ "
        f"{DIM}lamina{RESET} {DIM}{_meter(0.4)}{RESET} ─→ "
        f"KC {BLUE}{_meter(kc_fraction)}{RESET} {_num(neural.get('KC_spikes'), 0)} ─→ "
        f"MBON {AMBER}{_meter(changed / plastic)}{RESET} ×{_num(memory.get('mean_efficacy'))} ─→ "
        f"DNp20 ─→ {SIDE_COLOR.get(side, DIM)}{BOLD}▶ {side} ◀{RESET}"
    )
    stim_color = STIM_COLOR.get(stim, DIM)
    dopa = f"{stim_color}▓▓▓{RESET}" if stim != "none" else f"{DIM}───{RESET}"
    add(
        f"          ↑ {dopa} {STIM_CELL.get(stim, stim)}"
        + (f", {neural.get('stimulus_ms', 0):.0f} ms" if neural.get("stimulus_ms") else "")
        + f"   {DIM}pnl Δ{RESET} {_num(delta)} {quote_ccy}"
    )
    # --- decision scale (the actual decoder inputs) ------------------------
    left_hz = float(neural.get("left_hz") or 0)
    right_hz = float(neural.get("right_hz") or 0)
    diff = float(neural.get("difference_hz") or 0)
    reference = max(
        [abs(float((e.get("neural") or {}).get("difference_hz") or 0)) for e in history]
        + [abs(diff), 5.0]
    )
    rail = _scale_bar(diff, reference)
    add(f"DNp20 L {left_hz:.2f} Hz {rail} {right_hz:.2f} Hz DNp20 R")
    add(f"{DIM}Δ R−L{RESET} {GREEN if diff > 0 else RED if diff < 0 else DIM}{diff:+.2f} Hz{RESET}")
    gate = int(neural.get("gate_spikes") or 0)
    if gate:
        add(f"DNpe017 {GREEN}gate OPEN ({gate} spike(s)) → decoder may act{RESET}")
    else:
        add(f"DNpe017 {DIM}gate closed → decoder forced to HOLD{RESET}")
    exec_status = execution.get("status", "?")
    exec_color = DIM if exec_status == "HOLD" else (RED if exec_status == "VETO" else GREEN)
    add(f"execution {exec_color}{exec_status}{RESET}")
    if exec_status == "VETO" and execution.get("reason"):
        add(f"{AMBER}veto: {str(execution['reason'])[:width - 12]}{RESET}")

    # --- behaviour histogram ----------------------------------------------
    _rule(add, f"RECENT BEHAVIOUR ({min(len(history), TIMELINE_TICKS)} ticks)", width)
    recent = list(history)[-44:]
    lines.extend(_histogram(history, width=44, height=3))
    add("decision  " + "".join(_side_letter(e) for e in recent))
    add("stimulus  " + "".join(_stim_letter(e) for e in recent))
    add(f"{DIM}bars = |Δ R−L| · B buy  S sell  · hold | R reward  A aversive  - none{RESET}")

    add(
        f"{DIM}memory: {changed} / {plastic} plastic edges changed, mean efficacy ×{_num(memory.get('mean_efficacy'))} (candidate rule, unvalidated){RESET}"
    )
    add(f"{DIM}read-only observer · Ctrl-C exits · {DISCLAIMER}{RESET}")
    return "\n".join(line + "\x1b[K" for line in lines)


def _read_latest(path):
    try:
        return parse_event(Path(path).read_bytes())
    except OSError:
        return None


class _Tail:
    """Incremental reader for an append-only events.jsonl."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = self.path.stat().st_size if self.path.exists() else 0

    def poll(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            # Missing (mid-rotation, or the run has not created it yet): the
            # next recreation starts from zero, so no event is ever skipped.
            self.offset = 0
            return []
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return []
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read(size - self.offset)
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return []
        self.offset += cut + 1
        return [e for e in (parse_event(l) for l in chunk[:cut].split(b"\n")) if e]


def _draw(frame):
    sys.stdout.write("\x1b[H" + frame + "\x1b[J")
    sys.stdout.flush()


def _live(out, label):
    events_path = Path(out) / "events.jsonl"
    history = deque(maxlen=TIMELINE_TICKS)
    current = _read_latest(Path(out) / "latest.json")
    if current:
        history.append(current)
    status = read_status(out)
    _draw(render_frame(current, history, status, label=label))
    tail = _Tail(events_path)
    last_poll = time.monotonic()
    last_draw = last_poll
    while True:
        events = tail.poll()
        if events:
            for event in events:
                history.append(event)
            current = history[-1]
        now = time.monotonic()
        if now - last_poll >= 2.0:
            status = read_status(out)
            last_poll = now
        if events or now - last_draw >= 5.0:
            _draw(render_frame(current, history, status, label=label))
            last_draw = now
        time.sleep(0.4)


def _iter_events(path):
    """Stream parsed events one line at a time: O(1) memory regardless of the
    file size, never a whole-file ``read()``."""
    try:
        with Path(path).open("rb") as handle:
            for line in handle:
                event = parse_event(line.strip())
                if event:
                    yield event
    except OSError:
        return


def _replay(out, label, show_all, speed):
    path = Path(out) / "events.jsonl"
    history = deque(maxlen=TIMELINE_TICKS)
    status = read_status(out)
    delay = 1.0 / max(speed, 0.1)
    total = 0
    if show_all:
        # Stream the whole history: memory stays O(one tick), the file is
        # never accumulated.
        for event in _iter_events(path):
            total += 1
            history.append(event)
            _draw(render_frame(event, history, status, label=label, replay=True))
            time.sleep(delay)
        played = total
    else:
        # One streaming pass keeps only the bounded tail; the file itself is
        # never loaded.
        tail = deque(maxlen=REPLAY_WINDOW)
        for event in _iter_events(path):
            tail.append(event)
            total += 1
        played = len(tail)
        for event in tail:
            history.append(event)
            _draw(render_frame(event, history, status, label=label, replay=True))
            time.sleep(delay)
    if total == 0:
        _draw(_waiting_frame(label, status))
        while True:
            time.sleep(1)
    note = f"{BOLD}END OF REPLAY{RESET} {DIM}· {played}/{total} ticks · Ctrl-C exits{RESET}"
    while True:
        frame = render_frame(history[-1], history, status, label=label, replay=True)
        _draw(frame + "\n" + note + "\x1b[K")
        time.sleep(1)


def _enable_windows_vt():
    if os.name != "nt":
        return
    import ctypes

    kernel = ctypes.windll.kernel32
    mode = ctypes.c_uint32()
    handle = kernel.GetStdHandle(-11)
    if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel.SetConsoleMode(handle, mode.value | 0x0004)


def run(out, *, replay=False, show_all=False, speed=3.0):
    _enable_windows_vt()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    sys.stdout.flush()
    try:
        if replay:
            _replay(out, str(out), show_all, speed)
        else:
            _live(out, str(out))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\x1b[?1049l\x1b[0m")
        sys.stdout.flush()
    return 0
