"""Web-observer hardening: secure-by-default binding, payload redaction,
health endpoint, stalled-client timeout, duplicate-bind refusal, and a
missing webui asset degrading to a 503 instead of a connection reset."""

import json
import socket
import sqlite3
import threading
import urllib.request

import pytest

from stonkfly.web import RunState, make_server, serve
from stonkfly.watch import read_health


def sample_event(tick=0, side="BUY", stimulus="reward", settled=False):
    execution = (
        {"status": "SETTLED", "mode": "okx-demo", "client_order_id": "a" * 32}
        if settled
        else {"status": "HOLD"}
    )
    return {
        "tick": tick,
        "wall_time": 1789572960.9,
        "product": "BTC-USDT",
        "mode": "paper",
        "quote": {"bid": "100.0", "ask": "100.1"},
        "equity_usdt": "100.0",
        "pnl_delta_usdt": "0.0",
        "neural": {
            "side": side, "left_hz": 30.0, "right_hz": 36.0,
            "difference_hz": 6.0, "gate_spikes": 1, "stimulus": stimulus,
            "stimulus_ms": 200.0, "KC_spikes": 10, "total_spikes": 100,
            "brain_ms": 500.0,
            "memory": {"plastic_edges": 5, "changed_edges": 1,
                       "mean_efficacy": 1.0, "minimum_efficacy": 0.9},
        },
        "execution": execution,
    }


@pytest.fixture
def run_dir(tmp_path):
    with (tmp_path / "events.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(sample_event(tick=0, settled=True)) + "\n")
    (tmp_path / "latest.json").write_text(json.dumps(sample_event(tick=0, settled=True)))
    db = sqlite3.connect(tmp_path / "ledger.sqlite")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE orders (id TEXT PRIMARY KEY,status TEXT NOT NULL,created REAL NOT NULL,plan TEXT NOT NULL,exchange_id TEXT,settlement TEXT)")
    db.execute("INSERT INTO meta VALUES ('mode', '\"okx-demo\"')")
    db.execute("INSERT INTO meta VALUES ('tick', '1')")
    db.commit()
    db.close()
    return tmp_path


@pytest.fixture()
def server(run_dir):
    httpd = make_server("127.0.0.1", 0, RunState(run_dir))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url) as response:
        return response.status, response.read()


def test_payload_never_carries_the_client_order_id(run_dir):
    payload = RunState(run_dir).payload()
    raw = json.dumps(payload)
    assert "client_order_id" not in raw
    assert "a" * 32 not in raw
    # The decision and its outcome remain visible.
    assert payload["event"]["execution"]["status"] == "SETTLED"


def test_health_endpoint_reports_derived_state(server):
    url, _httpd = server
    status, body = get(url + "/api/health")
    assert status == 200
    health = json.loads(body)
    for key in ("worker", "network", "clock", "ledger",
                "consecutive_read_failures", "proxy"):
        assert key in health


def test_state_endpoint_still_serves_decisions(server):
    url, _httpd = server
    status, body = get(url + "/api/state")
    assert status == 200
    payload = json.loads(body)
    assert payload["event"]["execution"]["status"] == "SETTLED"
    assert "client_order_id" not in json.dumps(payload)


def test_missing_webui_asset_serves_503_not_a_reset(server, monkeypatch, capsys):
    import urllib.error

    import stonkfly.web as web

    url, _httpd = server
    monkeypatch.setattr(web, "WEBUI", web.Path("definitely/missing/webui.html"))
    try:
        get(url + "/")
        raise AssertionError("expected a 503")
    except urllib.error.HTTPError as error:
        assert error.code == 503
        assert error.read() == b"webui asset missing"
    assert "webui asset missing" in capsys.readouterr().err
    # The server is still alive afterwards.
    status, _body = get(url + "/api/health")
    assert status == 200


def test_default_bind_is_loopback():
    from stonkfly.cli import build_parser

    args = build_parser().parse_args(["serve"])
    assert args.host == "127.0.0.1"


def test_lan_bind_prints_a_loud_warning(run_dir, capsys):
    # serve() on a non-loopback address must print a prominent warning before
    # it starts. The thread runs forever (daemon), so only the banner matters.
    import contextlib
    import io
    import time

    buf = io.StringIO()
    thread = threading.Thread(
        target=serve, args=(run_dir, "0.0.0.0", 0), daemon=True
    )
    with contextlib.redirect_stdout(buf):
        thread.start()
        time.sleep(0.8)
    assert "NON-LOOPBACK" in buf.getvalue()


def test_second_server_on_a_live_port_refuses(run_dir):
    httpd = make_server("127.0.0.1", 0, RunState(run_dir))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(SystemExit, match="already listening"):
            serve(run_dir, "127.0.0.1", port)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_stalled_client_times_out_without_killing_the_server(
    run_dir, monkeypatch
):
    import stonkfly.web as web

    monkeypatch.setattr(web, "SOCKET_TIMEOUT", 0.5)
    httpd = make_server("127.0.0.1", 0, RunState(run_dir))
    # Rebind the handler timeout (class attribute was read at import).
    httpd.RequestHandlerClass.timeout = 0.5
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        # Connect and send nothing: the server must drop us, and survive.
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.settimeout(5)
        try:
            data = sock.recv(64)
        except ConnectionResetError:
            data = b""
        assert data == b""
        sock.close()
        # The server still answers new requests.
        status, body = get(f"http://127.0.0.1:{port}/api/health")
        assert status == 200
        assert b"worker" in body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_read_health_is_pure_observation(run_dir):
    before = (run_dir / "ledger.sqlite").stat().st_mtime_ns
    read_health(run_dir)
    after = (run_dir / "ledger.sqlite").stat().st_mtime_ns
    assert before == after  # no write, not even WAL recovery on the main db


def test_concurrent_payloads_share_one_serialized_event_reader(run_dir, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    state = RunState(run_dir)
    entered = threading.Event()
    second_started = threading.Event()
    overlap = threading.Event()
    release = threading.Event()
    calls = []

    def poll():
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            return [sample_event(tick=1)]
        if not release.is_set():
            overlap.set()
        return []

    def second_request():
        second_started.set()
        return state.payload()

    monkeypatch.setattr(state.tail, "poll", poll)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(state.payload)
        assert entered.wait(5)
        second = pool.submit(second_request)
        try:
            assert second_started.wait(5)
            assert not overlap.wait(0.2)
        finally:
            release.set()
        first.result(timeout=5)
        payload = second.result(timeout=5)
    assert [row["tick"] for row in payload["recent"]] == [0, 1]
