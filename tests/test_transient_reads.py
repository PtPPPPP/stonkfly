"""Failure-injection tests for the read-only availability policy.

At the transport level: GETs retry transient failures inside one bounded
budget, POSTs never retry, and the classifier separates "skip the tick" from
"halt". At the clock level: the exchange offset estimate is robust to jitter
and single-sample failure. Everything runs against in-memory doubles; no
socket is opened and no credential exists in this module.
"""

import pytest

from stonkfly.okx_client import (
    OKXBusinessError,
    OKXClient,
    OKXTransportError,
    TransientReadError,
    is_transient_read_failure,
)


class ScriptedTransport:
    """Plays a scripted sequence of outcomes per call; records everything."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client_with(script):
    transport = ScriptedTransport(script)
    return OKXClient(api_key="k", secret="s", passphrase="p", transport=transport), transport


def test_get_retries_transport_timeout_and_succeeds():
    # attempt 1 dies mid-request, attempt 2 answers: a read may retry.
    c, t = client_with(
        [OKXTransportError("OKX transport error"), (200, {"code": "0", "data": []})]
    )
    assert c.ticker("BTC-USDT") is None  # empty data on the successful attempt
    assert [m for m, _u in t.calls] == ["GET", "GET"]


def test_get_retries_retryable_http_status():
    c, t = client_with(
        [
            OKXTransportError("OKX HTTP error", status=502),
            OKXTransportError("OKX HTTP error", status=429),
            (200, {"code": "0", "data": []}),
        ]
    )
    assert c.ticker("BTC-USDT") is None
    assert len(t.calls) == 3


def test_get_does_not_retry_definitive_http_status():
    # 401 is an auth condition, not a transient: no second attempt.
    c, t = client_with([OKXTransportError("OKX HTTP error", status=401)])
    with pytest.raises(OKXTransportError):
        c.ticker("BTC-USDT")
    assert len(t.calls) == 1


def test_get_budget_is_bounded_and_exhaustion_raises():
    script = [OKXTransportError("OKX transport error") for _ in range(3)]
    c, t = client_with(script)
    with pytest.raises(OKXTransportError):
        c.ticker("BTC-USDT")
    assert len(t.calls) == 3  # exactly the bounded budget, never more


def test_post_is_single_shot_even_when_scripted_to_fail():
    # The one invariant that protects money: a POST is attempted exactly once,
    # whatever happens. The response may be lost; it must never be re-sent.
    c, t = client_with([OKXTransportError("OKX transport error")])
    with pytest.raises(OKXTransportError):
        c.place_order({"instId": "BTC-USDT"})
    assert [m for m, _u in t.calls] == ["POST"]


def test_classification_whitelist():
    assert is_transient_read_failure(TransientReadError("bad market payload"))
    assert is_transient_read_failure(OKXTransportError("connection lost"))
    assert is_transient_read_failure(OKXTransportError("gateway", status=502))
    assert is_transient_read_failure(OKXTransportError("rate", status=429))
    assert is_transient_read_failure(
        OKXBusinessError("balance: OKX code=51054", code="51054")
    )
    # Everything halt-worthy classifies as NOT transient:
    assert not is_transient_read_failure(OKXTransportError("denied", status=401))
    assert not is_transient_read_failure(
        OKXBusinessError("balance: OKX code=51000", code="51000")
    )
    assert not is_transient_read_failure(ValueError("program bug"))
    assert not is_transient_read_failure(RuntimeError("external balance change"))


def _fake_clock_client(samples):
    """A client double whose public_time serves canned ms timestamps."""

    class TimeSource:
        def __init__(self):
            self.i = 0

        def public_time(self):
            self.i += 1
            return samples[min(self.i - 1, len(samples) - 1)]

    class Client:
        def __init__(self):
            self.src = TimeSource()
            self.public_time = self.src.public_time

        measure_time_offset = OKXClient.measure_time_offset

    return Client()


def test_clock_offset_detects_local_clock_fast_and_slow():
    # Exchange says 1000.0 s; RTT is forced to 0 by the mono double, so the
    # offset is exactly exchange - local.
    fake = _fake_clock_client([1_000_000] * 5)
    m = fake.measure_time_offset(
        samples=5, local_clock=lambda: 1001.0, mono=lambda: 0.0
    )
    assert m["offset"] == pytest.approx(-1.0)
    fake = _fake_clock_client([1_000_000] * 5)
    m = fake.measure_time_offset(
        samples=5, local_clock=lambda: 999.0, mono=lambda: 0.0
    )
    assert m["offset"] == pytest.approx(1.0)


def test_clock_offset_midpoint_uses_half_the_rtt():
    # Documented estimator: server + rtt/2 - local_at_end. With a symmetric
    # round trip of 0.2 s the exchange instant sits one tenth into it.
    calls = {"n": 0}

    def mono():
        calls["n"] += 1
        return 0.0 if calls["n"] % 2 else 0.2  # t0=0.0, t1=0.2 per sample

    fake = _fake_clock_client([1_000_000] * 5)
    m = fake.measure_time_offset(
        samples=5, local_clock=lambda: 1000.0, mono=mono
    )
    assert m["offset"] == pytest.approx(0.1)
    assert m["rtt"] == pytest.approx(0.2)


def test_clock_offset_ignores_jittered_samples():
    # One sample takes 2.0 s and carries a 50 s-wrong server answer; the other
    # four are fast and exact. The low-RTT half must exclude the bad one, so a
    # single jittered request can never decide the estimate.
    class SlowFirst:
        def __init__(self):
            self.n = 0

        def public_time(self):
            self.n += 1
            return 1_050_000 if self.n == 1 else 1_000_000

        measure_time_offset = OKXClient.measure_time_offset

    seq = {"n": 0}

    def mono():
        # t0/t1 pairs: sample 1 spans 2.0 s, every later sample 0.02 s.
        seq["n"] += 1
        return 0.0 if seq["n"] % 2 else (2.0 if seq["n"] == 2 else 0.02)

    m = SlowFirst().measure_time_offset(samples=5, local_clock=lambda: 1000.0, mono=mono)
    # Kept half = 3 lowest-RTT samples, all exact -> offset 0, not +25.
    assert m["offset"] == pytest.approx(0.0, abs=0.05)


def test_clock_offset_failure_propagates():
    class Dead:
        def public_time(self):
            raise OKXBusinessError("public time: OKX code=51054", code="51054")

        measure_time_offset = OKXClient.measure_time_offset

    with pytest.raises(OKXBusinessError):
        Dead().measure_time_offset(samples=2, local_clock=lambda: 0.0, mono=lambda: 0.0)
