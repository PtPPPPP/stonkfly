"""Offline tests for the OKX public-endpoint probe script.

Only the pure helpers and output formatting are exercised here; no socket is
opened and no real OKX request leaves the process."""

import importlib.util
import os

import pytest

from stonkfly.okx_client import OKXTransportError

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "okx_probe.py",
)


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("okx_probe", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sanitize_proxy_strips_credentials(probe):
    assert probe.sanitize_proxy("127.0.0.1:7890") == "127.0.0.1:7890"
    assert probe.sanitize_proxy("http://127.0.0.1:7890") == "127.0.0.1:7890"
    assert probe.sanitize_proxy("user:pass@10.0.0.1:8080") == "10.0.0.1:8080"
    assert probe.sanitize_proxy("http://user:pass@10.0.0.1:8080") == "10.0.0.1:8080"
    assert probe.sanitize_proxy("") == "none"


def test_build_client_routes_through_the_proxy_without_credentials(probe):
    client = probe.build_client("127.0.0.1:7890")
    assert isinstance(client, probe.OKXClient)
    assert client.api_key is None  # no credentials are ever involved
    # the transport's proxy is set for this client, never a global default
    assert client._transport.proxy == "127.0.0.1:7890"


def test_probe_exposes_product_user_agent(probe):
    assert probe.USER_AGENT == "stonkfly/0.1.0"


def test_main_requires_proxy_arg(probe, capsys):
    assert probe.main([]) == 2
    assert probe.main(["okx_probe.py", ""]) == 2


def test_main_success_output_is_minimal(probe, monkeypatch, capsys):
    class FakeClient:
        def public_time(self):
            return 1700000000000

    monkeypatch.setattr(probe, "build_client", lambda proxy: FakeClient())
    rc = probe.main(["okx_probe.py", "127.0.0.1:7890"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "python: " in out
    assert "user_agent: stonkfly/0.1.0" in out
    assert "proxy: 127.0.0.1:7890" in out
    assert "elapsed_ms: " in out
    assert "success: true" in out
    # no body, header, signature, key, or account data may leak
    assert "OK-ACCESS" not in out
    assert '"data"' not in out


def test_main_failure_reports_http_status(probe, monkeypatch, capsys):
    class FailingClient:
        def public_time(self):
            raise OKXTransportError("OKX HTTP error", status=502)

    monkeypatch.setattr(probe, "build_client", lambda proxy: FailingClient())
    rc = probe.main(["okx_probe.py", "127.0.0.1:7890"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "success: false" in out
    assert "http_status: 502" in out


def test_main_connection_failure_reports_cause(probe, monkeypatch, capsys):
    class ConnClient:
        def public_time(self):
            raise OKXTransportError("OKX transport error") from TimeoutError("timed out")

    monkeypatch.setattr(probe, "build_client", lambda proxy: ConnClient())
    rc = probe.main(["okx_probe.py", "127.0.0.1:7890"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "success: false" in out
    assert "cause: TimeoutError" in out
    assert "http_status" not in out
