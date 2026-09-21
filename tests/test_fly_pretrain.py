"""Offline pretraining tool tests.

The fly runs for real here (full controller, plasticity, guard, paper ledger),
but the exchange is a fake transport serving synthetic candles, so no network is
touched and no order can leave the process.
"""

import hashlib
import importlib.util
import json
import os
import time

import pytest

from stonkfly.config import Settings
from stonkfly.market import Quote
from stonkfly import audit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tool():
    spec = importlib.util.spec_from_file_location(
        "fly_pretrain", os.path.join(ROOT, "tools", "fly_pretrain.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


INCREMENTS = {"lotSz": "0.00000001", "tickSz": "0.1", "minSz": "0.00001"}


class FakeOKX:
    """A public-API double: instruments plus one long candle series."""

    def __init__(self, closes):
        self.closes = closes
        now = int(time.time() * 1000)
        # newest-first rows, confirmed, one minute apart, ending at `now`
        self.rows = [
            [str(now - i * 60000), "1", "1", "1", str(c), "1", "1", "1", "1"]
            for i, c in enumerate(reversed(closes))
        ]
        self.page_requests = []

    def instruments(self, inst_id):
        assert inst_id == "BTC-USDT"
        return {"instId": "BTC-USDT", "lotSz": INCREMENTS["lotSz"],
                "tickSz": INCREMENTS["tickSz"], "minSz": INCREMENTS["minSz"]}

    def _get(self, path, params=None, auth=False, what="query", retries=3):
        # fetch_candles now goes through the client's retrying read path; the
        # double serves the same envelope straight from ``request``.
        return self.request("GET", path, params=params, auth=auth)

    def request(self, method, path, params=None, body=None, auth=False):
        assert method == "GET" and path in (
            "/api/v5/market/candles", "/api/v5/market/history-candles"
        )
        self.page_requests.append((path, params))
        after = int(params["after"]) if params and "after" in params else None
        rows = self.rows
        if after is not None:
            rows = [r for r in rows if int(r[0]) < after]
        return {"code": "0", "data": rows[:300]}


@pytest.fixture
def tool():
    return _tool()


def _seed_cache(tool, out, closes):
    out.mkdir(parents=True, exist_ok=True)
    (out / "candles.json").write_text(json.dumps({
        "product": "BTC-USDT", "bar": "1m", "complete": True,
        "increments": INCREMENTS, "closes": closes,
    }) + "\n")


def _closes(n, start=100.0):
    # a gentle sine wave: enough shape for the decoder to sometimes propose
    return [round(start + 5 * (i % 40) / 40 * (1 if (i // 40) % 2 == 0 else -1), 2)
            for i in range(n)]


def test_fetch_candles_paginates_and_orders_oldest_first(tool):
    fake = FakeOKX(_closes(700))
    closes, complete = tool.fetch_candles(fake, "BTC-USDT", "1m", 500)
    assert complete is True
    assert len(closes) == 500
    assert closes == _closes(700)[-500:]  # the newest 500, oldest first
    assert len(fake.page_requests) >= 2  # actually paged
    assert fake.page_requests[1][1]["after"]  # cursor pagination used


def test_fetch_candles_reports_a_truncated_scan(tool):
    fake = FakeOKX(_closes(50))
    closes, complete = tool.fetch_candles(fake, "BTC-USDT", "1m", 500)
    assert complete is False and len(closes) == 50


@pytest.mark.full_graph
def test_pretrain_runs_the_real_fly_end_to_end(tool, tmp_path, monkeypatch):
    """Ticks advance, checkpoints hash-verify, trades may settle, report written."""
    closes = _closes(200)
    _seed_cache(tool, tmp_path, closes)
    monkeypatch.setattr(tool, "build_client",
                        lambda: FakeOKX(closes))
    rc = tool.main(["--out", str(tmp_path), "--ticks", "6"])
    assert rc == 0

    s = tool.pretrain_settings("BTC-USDT")
    ledger = tool.Ledger(tmp_path / "ledger.sqlite", s, "paper")
    try:
        assert ledger.get("tick") == 6
        assert ledger.get("halted") is None
        cp = ledger.get("checkpoint")
        path = tmp_path / cp["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == cp["sha256"]
        obs = ledger.get("observation")
        assert obs["candle_cursor"] == 126            # next index after 6 consumed
        assert obs["virtual_now"] > 0
        # the observation carries the same window the frame was rendered from
        assert len(obs["market_history"]["BTC-USDT"]) == 120
    finally:
        ledger.close()

    report = json.loads((tmp_path / "report.json").read_text())
    assert report["ledger_tick"] == 6
    assert report["claim"] == "offline exploration; NOT validated profitable learning"


@pytest.mark.full_graph
def test_pretrain_resumes_from_the_ledger_without_refetching(tool, tmp_path):
    closes = _closes(300)
    _seed_cache(tool, tmp_path, closes)
    tool.main(["--out", str(tmp_path), "--ticks", "3"])
    ledger_before = audit.read_ledger(tmp_path)
    cursor_before = ledger_before["meta"]["observation"]["candle_cursor"]
    assert cursor_before == 123                  # next index after 3 consumed

    rc = tool.main(["--out", str(tmp_path), "--ticks", "3"])
    assert rc == 0
    # resume continues the cursor instead of restarting the window
    view = audit.read_ledger(tmp_path)
    obs = view["meta"]["observation"]
    assert obs["candle_cursor"] == cursor_before + 3
    assert view["meta"]["tick"] == 6
    # the checkpoint still verifies after the second run
    cp = view["meta"]["checkpoint"]
    path = tmp_path / cp["file"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == cp["sha256"]


@pytest.mark.full_graph
def test_pretrain_trades_under_the_virtual_clock(tool, tmp_path, monkeypatch):
    """When the decoder proposes, the paper fill settles with the demo's fee rate.

    Nothing is scripted: the decoder proposes what it proposes over these
    ticks; the assertions cover the bookkeeping of any proposal and the live
    daily cap, which the virtual clock reproduces.
    """
    closes = _closes(300)
    _seed_cache(tool, tmp_path, closes)
    monkeypatch.setattr(tool, "build_client", lambda: FakeOKX(closes))
    tool.main(["--out", str(tmp_path), "--ticks", "8"])
    view = audit.read_ledger(tmp_path)
    orders = view["orders"]
    for order in orders:
        assert order["status"] in ("SETTLED", "REJECTED")
        # created timestamps are virtual: strictly 60 virtual seconds apart
        assert order["plan"]  # sized by the live guard's formula
    # the live daily cap survives: at most 24 orders per virtual day
    created = [o["created"] for o in orders]
    for c in created:
        day = [x for x in created if x - (c - c % 86400) >= 0 and x - (c - c % 86400) < 86400]
        assert len(day) <= Settings(products=("BTC-USDT",)).daily_orders


@pytest.mark.full_graph
def test_pretrain_stops_at_the_loss_stop(tool, tmp_path):
    """The same 20 USDT drawdown rule that halts a live run halts a pretrain."""
    closes = _closes(200)
    _seed_cache(tool, tmp_path, closes)
    tool.main(["--out", str(tmp_path), "--ticks", "1"])
    # seed the state a deep drawdown would leave: equity ~50 vs the 80 floor
    s = tool.pretrain_settings("BTC-USDT")
    ledger = tool.Ledger(tmp_path / "ledger.sqlite", s, "paper")
    ledger.put("cash", "50")
    ledger.put("positions", {"BTC-USDT": "0.0006"})
    ledger.close()

    rc = tool.main(["--out", str(tmp_path), "--ticks", "5"])
    # The loss-stop halt is a real halt: the exit code must fail (it used to
    # report success even when the ledger stopped).
    assert rc == 1
    view = audit.read_ledger(tmp_path)
    assert "Loss stop" in str(view["meta"]["halted"])
    # the guard refuses before any further tick commits
    assert view["meta"]["tick"] == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert "Loss stop" in str(report["halted"])


def test_pretrain_requires_enough_history(tool, tmp_path, monkeypatch):
    _seed_cache(tool, tmp_path, _closes(50))  # stale: fewer than requested
    monkeypatch.setattr(tool, "build_client", lambda: FakeOKX(_closes(50)))
    # A missing dataset fails the run cleanly (exit 1 + message), it does not
    # raise a traceback out of main.
    rc = tool.main(["--out", str(tmp_path), "--ticks", "1", "--refresh"])
    assert rc == 1


def test_pretrain_settings_match_the_live_protocol_except_the_fee(tool):
    s = tool.pretrain_settings("BTC-USDT")
    live = Settings(products=("BTC-USDT",))
    assert s.products == live.products
    assert s.capital == live.capital
    assert s.order_limit == live.order_limit
    assert s.loss_stop == live.loss_stop
    assert s.daily_orders == live.daily_orders
    assert s.reward_deadband == live.reward_deadband
    assert s.decoder_threshold_hz == live.decoder_threshold_hz
    assert s.learning == live.learning
    # the one override: the offline fill cost matches the demo's 0.1% taker fee
    assert s.paper_fee == "0.001"


def test_deploy_replaces_and_records(tmp_path):
    tool = _tool()
    # source: a pretrain-style dir with a checkpoint
    source = tmp_path / "pre"
    source.mkdir()
    s = Settings(products=("BTC-USDT",))
    sl = tool.Ledger(source / "ledger.sqlite", s, "paper")
    (source / "brain-0.npz").write_bytes(b"pretrained neural state")
    sl.put("checkpoint", {
        "file": "brain-0.npz",
        "sha256": hashlib.sha256(b"pretrained neural state").hexdigest(),
    })
    sl.put("tick", 500)
    sl.close()

    # target: an okx-demo run directory with a live checkpoint to replace
    target = tmp_path / "v2"
    from stonkfly.ledger import Ledger

    tl = tool.Ledger(target / "ledger.sqlite", s, "okx-demo",
                     identity={"exchange": "okx", "environment": "okx-demo",
                               "account": "uid-1", "quote_ccy": "USDT"})
    (target / "brain-1.npz").write_bytes(b"current live brain")
    replaced = {"file": "brain-1.npz",
                "sha256": hashlib.sha256(b"current live brain").hexdigest()}
    tl.put("checkpoint", replaced)
    tl.close()

    assert tool.main(["--out", str(source), "--deploy-to", str(target)]) == 0

    name = "brain-pretrained-" + hashlib.sha256(b"pretrained neural state").hexdigest() + ".npz"
    deployed = target / name
    assert deployed.read_bytes() == b"pretrained neural state"
    view = audit.read_ledger(target)
    assert view["meta"]["checkpoint"] == {
        "file": name,
        "sha256": hashlib.sha256(b"pretrained neural state").hexdigest(),
    }
    trail = view["meta"]["checkpoint_deployments"]
    assert trail[0]["replaced"] == replaced
    assert trail[0]["source_tick"] == 500
    assert "NOT validated profitable learning" in trail[0]["note"]


def test_deploy_refuses_a_target_with_an_unresolved_intent(tmp_path):
    tool = _tool()
    source = tmp_path / "pre"
    source.mkdir()
    s = Settings(products=("BTC-USDT",))
    sl = tool.Ledger(source / "ledger.sqlite", s, "paper")
    (source / "brain-0.npz").write_bytes(b"state")
    sl.put("checkpoint", {"file": "brain-0.npz",
                          "sha256": hashlib.sha256(b"state").hexdigest()})
    sl.close()

    from stonkfly.ledger import Ledger

    target = tmp_path / "v2"
    tl = tool.Ledger(target / "ledger.sqlite", s, "okx-demo",
                     identity={"exchange": "okx", "environment": "okx-demo",
                               "account": "uid-1", "quote_ccy": "USDT"})
    tl.db.execute(
        "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
        ("a" * 32, "UNKNOWN", time.time(), json.dumps({"product": "BTC-USDT"})),
    )
    tl.close()

    assert tool.main(["--out", str(source), "--deploy-to", str(target)]) == 1
    assert not (target / "brain-pretrained.npz").exists()
    view = audit.read_ledger(target)
    assert view["meta"].get("checkpoint_deployments") is None


def test_deploy_verifies_the_source_hash(tmp_path):
    tool = _tool()
    source = tmp_path / "pre"
    source.mkdir()
    s = Settings(products=("BTC-USDT",))
    sl = tool.Ledger(source / "ledger.sqlite", s, "paper")
    (source / "brain-0.npz").write_bytes(b"tampered content")
    sl.put("checkpoint", {"file": "brain-0.npz",
                          "sha256": hashlib.sha256(b"recorded elsewhere").hexdigest()})
    sl.close()

    from stonkfly.ledger import Ledger

    target = tmp_path / "v2"
    tool.Ledger(target / "ledger.sqlite", s, "okx-demo",
                identity={"exchange": "okx", "environment": "okx-demo",
                          "account": "uid-1", "quote_ccy": "USDT"}).close()

    assert tool.main(["--out", str(source), "--deploy-to", str(target)]) == 1
    assert not (target / "brain-pretrained.npz").exists()


def _deployment_pair(tool, tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    settings = Settings(products=("BTC-USDT",))
    for path, mode, content in (
        (source, "paper", b"new state"), (target, "okx-demo", b"committed state"),
    ):
        ledger = tool.Ledger(path / "ledger.sqlite", settings, mode)
        (path / "brain-0.npz").write_bytes(content)
        ledger.put("checkpoint", {"file": "brain-0.npz",
                                  "sha256": hashlib.sha256(content).hexdigest()})
        ledger.close()
    return source, target


@pytest.mark.parametrize("owner", ["source", "target"])
def test_deploy_refuses_before_reading_a_locked_directory(tmp_path, monkeypatch, owner):
    from stonkfly import locking

    tool = _tool()
    source, target = _deployment_pair(tool, tmp_path)
    directory = source if owner == "source" else target
    lock = locking.acquire(directory / "worker.lock")
    try:
        monkeypatch.setattr(audit, "read_ledger", lambda p: pytest.fail("read before lock"))
        assert tool.deploy(source, target) == 1
    finally:
        locking.release(lock)


def test_failed_deployment_keeps_the_committed_checkpoint(tmp_path, monkeypatch):
    from pathlib import Path

    tool = _tool()
    source, target = _deployment_pair(tool, tmp_path)
    before = audit.read_ledger(target)["meta"]["checkpoint"]
    real_replace = Path.replace

    def fail_replace(path, destination):
        if path.parent == target and path.suffix == ".partial":
            raise OSError("injected disk failure")
        return real_replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="injected disk failure"):
        tool.deploy(source, target)
    after = audit.read_ledger(target)["meta"]
    assert after["checkpoint"] == before
    assert (target / before["file"]).read_bytes() == b"committed state"
    assert after.get("checkpoint_deployments") is None


def test_pretrain_refuses_a_second_worker_before_fetching(tmp_path, monkeypatch):
    from stonkfly import locking

    tool = _tool()
    lock = locking.acquire(tmp_path / "worker.lock")
    try:
        monkeypatch.setattr(tool, "build_client", lambda: pytest.fail("client created before lock"))
        assert tool.main(["--out", str(tmp_path), "--ticks", "1"]) == 1
    finally:
        locking.release(lock)


def test_daily_cap_counts_only_actual_fills(tool, tmp_path):
    """The daily rate limit is fill-based: rejections and zero-fill FOK
    cancellations do not consume it; real fills do."""
    from stonkfly.config import D

    s = Settings(products=("BTC-USDT",))
    l = tool.Ledger(tmp_path / "l.sqlite", s, "paper")
    now = time.time()
    day = now - now % 86400

    def order(cid, status, created, base=None):
        settlement = (
            json.dumps({"base": base, "quote": "1", "fee": "0", "fee_ccy": "quote"})
            if base is not None
            else None
        )
        l.db.execute(
            "INSERT INTO orders(id,status,created,plan,exchange_id,settlement)"
            " VALUES (?,?,?,?,?,?)",
            (cid, status, created, json.dumps({"product": "BTC-USDT"}),
             "ord-1" if status == "SETTLED" else None, settlement),
        )

    order("a" * 32, "REJECTED", day + 1)                      # definite rejection
    order("b" * 32, "SETTLED", day + 2, base="0")             # zero-fill FOK cancel
    order("c" * 32, "SETTLED", day + 3, base="0.0001")        # a real fill
    order("d" * 32, "SETTLED", day - 86400 - 1, base="0.5")   # yesterday's fill

    assert l.filled_today(now) == 1  # only the real fill, only today's
    l.close()


def test_fetch_walks_recent_then_history_endpoints(tool):
    """Deep history: the recent window is stitched with the archive pages."""
    closes = _closes(2000)
    now = int(time.time() * 1000)
    calls = []

    class SplitSource:
        """Emulates the real split: /market/candles serves only the newest
        ~1440 1m bars, /history-candles serves everything older, 100/page."""

        def _get(self, path, params=None, auth=False, what="query", retries=3):
            return self.request("GET", path, params=params, auth=auth)

        def __init__(self):
            calls.append("init")
            newest = list(enumerate(closes[:1440]))       # recent endpoint's reach
            older = list(enumerate(closes[1440:], start=1440))
            self.rows = []
            for i, c in reversed(newest):                 # newest first
                self.rows.append((now - i * 60000, c))
            for i, c in reversed(older):
                self.rows.append((now - i * 60000, c))
            self.rows.sort(reverse=True)                  # (ts, close) newest first

        def request(self, method, path, params=None, body=None, auth=False):
            calls.append(path)
            after = int(params["after"]) if params and "after" in params else None
            page = 300 if "history" not in path else 100
            rows = [r for r in self.rows if after is None or r[0] < after]
            if "history" not in path:
                rows = [r for r in rows if r[0] >= now - 1440 * 60000]
            rows = sorted(rows, reverse=True)[:page]
            data = [
                [str(ts), "1", "1", "1", str(c), "1", "1", "1", "1"]
                for ts, c in rows
            ]
            return {"code": "0", "data": data}

    fake = SplitSource()
    tool_client = type("C", (), {
        "instruments": staticmethod(lambda p: {
            "instId": "BTC-USDT", "lotSz": INCREMENTS["lotSz"],
            "tickSz": INCREMENTS["tickSz"], "minSz": INCREMENTS["minSz"]}),
        "_get": lambda self, path, params=None, auth=False, what="query", retries=3:
            fake.request("GET", path, params, auth),
        "request": lambda self, method, path, params=None, body=None, auth=False:
            fake.request(method, path, params, body, auth),
    })()
    closes, complete = tool.fetch_candles(tool_client, "BTC-USDT", "1m", 2000)
    assert complete is True and len(closes) == 2000
    assert closes == closes[:]      # oldest first, no duplicates
    assert len(set(closes)) >= 2
    joined = " ".join(str(c) for c in calls)
    assert "market/history-candles" in joined   # the archive was walked
    assert joined.count("market/candles") >= 2  # after the recent window too
