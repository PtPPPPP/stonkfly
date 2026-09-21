"""watch --replay must never load the whole events file into memory: it
streams line by line and keeps only the bounded replay window."""

import json
import time

import pytest

from stonkfly.watch import REPLAY_WINDOW, _iter_events, _replay


def _write_events(path, n):
    with path.open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(
                json.dumps(
                    {
                        "tick": i,
                        "wall_time": 1789572960.9 + i,
                        "product": "BTC-USDT",
                        "mode": "paper",
                        "quote": {"bid": "100", "ask": "100.1"},
                        "neural": {"side": "HOLD", "KC_spikes": 0,
                                   "stimulus": "none", "left_hz": 0.0,
                                   "right_hz": 0.0, "difference_hz": 0.0,
                                   "gate_spikes": 0,
                                   "memory": {"plastic_edges": 1,
                                              "changed_edges": 0}},
                        "execution": {"status": "HOLD"},
                    }
                )
                + "\n"
            )


def test_iter_events_streams_every_line(tmp_path):
    _write_events(tmp_path / "events.jsonl", 500)
    events = list(_iter_events(tmp_path / "events.jsonl"))
    assert len(events) == 500
    assert events[0]["tick"] == 0 and events[-1]["tick"] == 499


def test_iter_events_survives_torn_lines_and_missing_files(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"tick": 1}\n{"tick": 2\n{"tick": 3}\n')  # middle torn
    events = list(_iter_events(path))
    assert [e["tick"] for e in events] == [1, 3]
    assert list(_iter_events(tmp_path / "absent.jsonl")) == []


def test_replay_default_window_is_bounded(tmp_path, monkeypatch):
    """100k events, only REPLAY_WINDOW drawn: the streaming pass touches every
    line once (O(N) time) but memory stays O(window)."""
    n = 100_000
    _write_events(tmp_path / "events.jsonl", n)
    seen = []

    def fake_draw(frame):
        seen.append(frame)

    sleeps = {"n": 0}

    def fake_sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] > REPLAY_WINDOW:
            raise KeyboardInterrupt  # first sleep of the end-of-replay loop

    monkeypatch.setattr("stonkfly.watch._draw", fake_draw)
    monkeypatch.setattr("stonkfly.watch.time.sleep", fake_sleep)
    monkeypatch.setattr("stonkfly.watch.time.strftime", lambda *a: "00:00:00")

    with pytest.raises(KeyboardInterrupt):
        _replay(tmp_path, "test", show_all=False, speed=10_000)

    # One draw per replayed event plus the final note frame -- never n.
    assert len(seen) <= REPLAY_WINDOW + 2
    assert sleeps["n"] <= REPLAY_WINDOW + 6


def test_replay_show_all_streams_without_accumulating(tmp_path, monkeypatch):
    """--all replays as it streams: draws happen per line, memory O(1)."""
    n = 5_000
    _write_events(tmp_path / "events.jsonl", n)
    draws = {"n": 0}

    def fake_draw(frame):
        draws["n"] += 1
        if draws["n"] > n:  # the end-of-replay loop
            raise KeyboardInterrupt

    def fake_sleep(_s):
        if draws["n"] > n:
            raise KeyboardInterrupt

    monkeypatch.setattr("stonkfly.watch._draw", fake_draw)
    monkeypatch.setattr("stonkfly.watch.time.sleep", fake_sleep)
    monkeypatch.setattr("stonkfly.watch.time.strftime", lambda *a: "00:00:00")

    with pytest.raises(KeyboardInterrupt):
        _replay(tmp_path, "test", show_all=True, speed=10_000)
    assert draws["n"] == n + 1  # every event drawn exactly once, then the note
