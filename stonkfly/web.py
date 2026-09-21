"""Read-only LAN view of a fly run: one static page plus JSON state polling.

Serves the same read-only data the terminal watcher consumes: ``latest.json``,
the ``events.jsonl`` tail, and run metadata read from ``ledger.sqlite`` over a
read-only connection. Nothing is written to the run directory, only GET is
served. Equity trends are visible; account identifiers, cash, positions and
credentials are not exposed.
"""

import json
import socket
import sys
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .watch import (
    TIMELINE_TICKS,
    _read_latest,
    _Tail,
    parse_event,
    read_health,
    read_status,
)

WEBUI = Path(__file__).with_name("webui.html")

# Per-connection read timeout: a client that connects but never completes a
# request (stalled tab, port scanner) is dropped instead of pinning a thread
# forever. One bad client never takes the server down.
SOCKET_TIMEOUT = 30.0

_webui_missing_warned = False


def _sanitize(event):
    """Observer payload redaction. The client order id identifies our orders
    on the exchange and has no business in a read-only view; equity stays
    (the UI renders it by design), but order identifiers do not leave.
    """
    if not isinstance(event, dict):
        return event
    clean = dict(event)
    execution = clean.get("execution")
    if isinstance(execution, dict) and "client_order_id" in execution:
        clean["execution"] = {
            k: v for k, v in execution.items() if k != "client_order_id"
        }
    return clean


class RunState:
    """Incrementally tracked view of one run directory."""

    def __init__(self, out):
        self.out = Path(out)
        self._lock = threading.Lock()
        self.recent = deque(maxlen=TIMELINE_TICKS)
        try:
            with (self.out / "events.jsonl").open("rb") as handle:
                for line in handle:
                    event = parse_event(line)
                    if event:
                        self.recent.append(event)
        except OSError:
            pass
        self.tail = _Tail(self.out / "events.jsonl")

    def refresh(self):
        for event in self.tail.poll():
            self.recent.append(event)

    def prices(self, product, status):
        prices = (status.get("market_history") or {}).get(product)
        if isinstance(prices, list) and len(prices) >= 2:
            return prices
        collected = [
            float(e["quote"]["bid"])
            for e in self.recent
            if e.get("product") == product
            and (e.get("quote") or {}).get("bid") is not None
        ]
        return collected if len(collected) >= 2 else (prices if isinstance(prices, list) else [])

    def payload(self):
        # HTTP handlers share this reader. Advancing its file offset and
        # iterating its deque must form one snapshot across concurrent polls.
        with self._lock:
            return self._payload()

    def _payload(self):
        self.refresh()
        status = read_status(self.out)
        event = _read_latest(self.out / "latest.json")
        if event is None and self.recent:
            event = self.recent[-1]
        product = (event or {}).get("product") or ""
        return {
            "label": str(self.out),
            "status": {k: status.get(k) for k in ("mode", "tick", "halted")},
            "event": _sanitize(event),
            "prices": self.prices(product, status),
            "recent": [
                {
                    "tick": e.get("tick"),
                    "side": (e.get("neural") or {}).get("side"),
                    "stimulus": (e.get("neural") or {}).get("stimulus"),
                    "difference_hz": (e.get("neural") or {}).get("difference_hz"),
                    "equity": next(
                        (v for k, v in e.items() if k.startswith("equity_")), None
                    ),
                }
                for e in self.recent
            ],
        }


class WatchHandler(BaseHTTPRequestHandler):
    server_version = "StonkflyWatch/1"
    timeout = SOCKET_TIMEOUT
    state = None

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                body = WEBUI.read_bytes()
            except OSError:
                global _webui_missing_warned
                if not _webui_missing_warned:
                    print("webui asset missing; serving 503 for the page",
                          file=sys.stderr, flush=True)
                    _webui_missing_warned = True
                self._send(503, "text/plain; charset=utf-8", b"webui asset missing")
                return
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/api/state":
            body = json.dumps(self.state.payload(), allow_nan=False).encode()
            self._send(200, "application/json", body)
        elif self.path == "/api/health":
            body = json.dumps(read_health(self.state.out), allow_nan=False).encode()
            self._send(200, "application/json", body)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class StonkflyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A timed-out, reset or malformed connection is routine observation
        # noise: drop it quietly. Anything unexpected still reports.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(host, port, state):
    handler = type("BoundHandler", (WatchHandler,), {"state": state})
    return StonkflyHTTPServer((host, port), handler)


def _port_has_listener(host, port):
    """True when something accepts connections on host:port right now.

    Windows binds a second SO_REUSEADDR listener successfully even while
    another server owns the port (verified on this machine), which would leave
    two silent servers with nondeterministic routing. A connect probe detects
    a live listener; a dead port in TIME_WAIT refuses the connection, so a
    normal restart is unaffected.
    """
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        with socket.create_connection((probe_host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _lan_addresses(host, port):
    if host not in ("", "0.0.0.0", "::"):
        return [f"http://{host}:{port}"]
    addresses = ["localhost"]
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            addresses.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    return [f"http://{address}:{port}" for address in dict.fromkeys(addresses)]


def serve(out, host="127.0.0.1", port=8400):
    if host in ("", "0.0.0.0", "::"):
        print(
            "WARNING: serving the read-only view on a NON-LOOPBACK address; "
            "anyone on this network can read the fly's decisions and equity "
            "trend. Use 127.0.0.1 (default) to keep it on this machine.",
            flush=True,
        )
    if _port_has_listener(host, port):
        raise SystemExit(
            f"Cannot serve {host}:{port}: something is already listening there"
        )
    state = RunState(out)
    try:
        httpd = make_server(host, port, state)
    except OSError as error:
        raise SystemExit(f"Cannot bind {host}:{port}: {error}") from None
    print(
        f"Stonkfly watch: read-only view of {out}\n"
        + "\n".join(f"  {url}" for url in _lan_addresses(host, port))
        + "\nCtrl-C stops serving.",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
