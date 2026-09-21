"""Watch renderer and tailing tests; no terminal required."""

import json
import sqlite3

from stonkfly.watch import (
    _Tail,
    parse_event,
    read_status,
    render_frame,
    sparkline,
)


def sample_event(tick=1, side="HOLD", stimulus="none", gate=1, execution="HOLD"):
    return {
        "tick": tick,
        "wall_time": 1789572960.9,
        "product": "BTC-USDT",
        "mode": "paper",
        "quote": {"bid": "87310.5", "ask": "87311.0"},
        "equity_usdt": "100.0",
        "pnl_delta_usdt": "0.0",
        "neural": {
            "side": side,
            "left_hz": 30.0,
            "right_hz": 36.0,
            "difference_hz": 6.0,
            "gate_spikes": gate,
            "stimulus": stimulus,
            "stimulus_ms": 200.0,
            "reward_spikes": 0,
            "aversive_spikes": 117,
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
        "execution": {"status": execution},
        "market_history": {"BTC-USDT": [100.0, 101.0, 99.0]},
    }


def render(event, **kwargs):
    return render_frame(event, [event], {}, **kwargs)


def test_sparkline_empty():
    assert sparkline([]) == ""


def test_sparkline_flat_is_a_bar_row():
    row = sparkline([5.0, 5.0, 5.0])
    assert len(row) == 3
    assert len(set(row)) == 1


def test_sparkline_tracks_order():
    low = sparkline([0.0, 10.0])
    assert low[0] < low[1]


def test_parse_event_rejects_garbage():
    assert parse_event(b"not json") is None
    assert parse_event(b"[1, 2]") is None
    assert parse_event(b'{"tick": 1}') == {"tick": 1}


def test_render_shows_neural_pipeline():
    frame = render(sample_event(side="BUY", stimulus="aversive"))
    for expected in (
        "STONKFLY WATCH",
        "BTC-USDT",
        "DNp20 L",
        "DNp20 R",
        "DNpe017",
        "BUY",
        "PPL101",
        "unvalidated",
        "not validated",
    ):
        assert expected in frame


def test_render_flow_connects_stages():
    frame = render(sample_event(side="BUY", stimulus="reward"))
    for expected in ("eyes", "lamina", "KC", "MBON", "DNp20", "PAM11"):
        assert expected in frame


def test_render_hold_with_closed_gate():
    frame = render(sample_event(side="HOLD", gate=0))
    assert "HOLD" in frame
    assert "forced to HOLD" in frame


def test_render_veto_reason():
    frame = render(
        sample_event(side="BUY", execution="VETO")
        | {"execution": {"status": "VETO", "reason": "Price moved"}}
    )
    assert "VETO" in frame
    assert "Price moved" in frame


def test_render_waiting_frame_without_events():
    frame = render_frame(None, [], {}, label="runs/paper")
    assert "WAITING FOR EVENTS" in frame
    assert "not validated" in frame


def test_render_timeline_counts_letters():
    events = [
        sample_event(tick=i, side="BUY" if i % 2 else "HOLD") for i in range(5)
    ]
    frame = render_frame(events[-1], events, {})
    assert frame.count("B") >= 2
    assert "RECENT BEHAVIOUR" in frame


def test_tail_streams_complete_lines_only(tmp_path):
    path = tmp_path / "events.jsonl"
    # The tail follows lines appended after construction; the latest state at
    # startup comes from latest.json, not from replaying the file.
    tail = _Tail(path)
    assert tail.poll() == []
    path.write_text(json.dumps(sample_event(tick=0)) + "\n")
    assert [e["tick"] for e in tail.poll()] == [0]
    assert tail.poll() == []
    # A partially written line is held back until it is complete.
    with path.open("a") as handle:
        handle.write(json.dumps(sample_event(tick=1))[:20])
    assert tail.poll() == []
    with path.open("a") as handle:
        handle.write(json.dumps(sample_event(tick=1))[20:] + "\n")
    assert [e["tick"] for e in tail.poll()] == [1]


def test_read_status_absent_database(tmp_path):
    assert read_status(tmp_path) == {}


def test_read_status_reads_meta(tmp_path):
    db = sqlite3.connect(tmp_path / "ledger.sqlite")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO meta VALUES ('mode', '\"paper\"')")
    db.execute("INSERT INTO meta VALUES ('halted', '\"Loss stop\"')")
    db.commit()
    db.close()
    status = read_status(tmp_path)
    assert status["mode"] == "paper"
    assert status["halted"] == "Loss stop"


def test_read_status_surfaces_market_history(tmp_path):
    db = sqlite3.connect(tmp_path / "ledger.sqlite")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    observation = json.dumps({"market_history": {"BTC-USDT": [1.0, 2.0]}})
    db.execute(
        "INSERT INTO meta VALUES ('observation', ?)", (observation,)
    )
    db.commit()
    db.close()
    status = read_status(tmp_path)
    assert status["market_history"]["BTC-USDT"] == [1.0, 2.0]


def test_render_uses_status_price_history_over_events(tmp_path):
    event = sample_event()
    status = {"market_history": {"BTC-USDT": [10.0, 20.0, 30.0]}}
    frame = render_frame(event, [event], status)
    # The event stream alone would leave the chart empty (one price point);
    # the status-supplied history must be what gets drawn.
    chart_rows = frame.splitlines()[3:11]
    assert any("█" in line for line in chart_rows)


def test_render_equity_sparkline_uses_events():
    events = [sample_event(tick=i) for i in range(4)]
    frame = render_frame(events[-1], events, {})
    assert sparkline([100.0] * 4, 24) in frame
