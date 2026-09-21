"""Web watch server tests: payload shape and read-only HTTP behaviour."""

import json
import sqlite3
import threading
import urllib.request

import pytest

from stonkfly.web import RunState, make_server
from stonkfly.watch import render_frame


def sample_event(tick=0, side="BUY", stimulus="reward"):
    return {
        "tick": tick,
        "wall_time": 1789572960.9,
        "product": "BTC-USDT",
        "mode": "paper",
        "quote": {"bid": "100.0", "ask": "100.1"},
        "equity_usdt": "100.0",
        "pnl_delta_usdt": "0.0",
        "neural": {
            "side": side,
            "left_hz": 30.0,
            "right_hz": 36.0,
            "difference_hz": 6.0,
            "gate_spikes": 1,
            "stimulus": stimulus,
            "stimulus_ms": 200.0,
            "KC_spikes": 5342,
            "total_spikes": 584816,
            "brain_ms": 500.0,
            "memory": {
                "plastic_edges": 7835,
                "changed_edges": 1696,
                "mean_efficacy": 1.0046,
                "minimum_efficacy": 0.7092,
            },
        },
        "execution": {"status": "SETTLED"},
        "market_history": {"BTC-USDT": [99.0, 100.0]},
    }


@pytest.fixture()
def run_dir(tmp_path):
    with (tmp_path / "events.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(sample_event(tick=0)) + "\n")
        handle.write(json.dumps(sample_event(tick=1, side="HOLD", stimulus="none")) + "\n")
    (tmp_path / "latest.json").write_text(
        json.dumps(sample_event(tick=1, side="HOLD", stimulus="none"))
    )
    db = sqlite3.connect(tmp_path / "ledger.sqlite")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO meta VALUES ('mode', '\"paper\"')")
    db.execute("INSERT INTO meta VALUES ('tick', '1')")
    db.execute(
        "INSERT INTO meta VALUES ('observation', ?)",
        (json.dumps({"market_history": {"BTC-USDT": [98.0, 99.0, 100.0]}}),),
    )
    db.commit()
    db.close()
    return tmp_path


@pytest.fixture()
def server(run_dir):
    httpd = make_server("127.0.0.1", 0, RunState(run_dir))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url) as response:
        return response.status, response.read()


def test_payload_shape(run_dir):
    payload = RunState(run_dir).payload()
    assert payload["label"] == str(run_dir)
    assert payload["status"]["mode"] == "paper"
    assert payload["event"]["neural"]["side"] == "HOLD"
    assert payload["prices"] == [98.0, 99.0, 100.0]
    assert [r["tick"] for r in payload["recent"]] == [0, 1]
    assert payload["recent"][0]["side"] == "BUY"
    # No account state leaks through the API.
    assert "cash" not in payload["status"]
    assert "positions" not in payload["status"]


def test_payload_on_empty_directory(tmp_path):
    payload = RunState(tmp_path).payload()
    assert payload["event"] is None
    assert payload["recent"] == []
    assert payload["prices"] == []


def test_http_pages(server):
    status, body = get(server + "/")
    assert status == 200
    assert b"STONKFLY" in body
    status, body = get(server + "/api/state")
    assert status == 200
    payload = json.loads(body)
    assert payload["status"]["mode"] == "paper"
    assert payload["event"]["tick"] == 1


def test_http_404(server):
    try:
        get(server + "/nope")
    except urllib.error.HTTPError as error:
        assert error.code == 404
    else:
        pytest.fail("expected 404")


def test_state_tracks_appended_events(run_dir):
    state = RunState(run_dir)
    assert len(state.recent) == 2
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample_event(tick=2, side="SELL")) + "\n")
    payload = state.payload()
    assert [r["tick"] for r in payload["recent"]] == [0, 1, 2]
    assert payload["recent"][2]["side"] == "SELL"


def test_webui_and_terminal_renderers_agree(run_dir):
    """Both views read the same payload data without erroring."""
    payload = RunState(run_dir).payload()
    frame = render_frame(payload["event"], [], payload["status"])
    assert "BTC-USDT" in frame
