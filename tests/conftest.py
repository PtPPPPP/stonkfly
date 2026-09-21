"""Keep ordinary execution tests independent of the downloaded connectome."""

import os
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolate_local_credentials(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for name in (
        "OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE",
        "COINBASE_KEY_FILE", "COINBASE_PORTFOLIO_ID", "STONKFLY_LIVE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def demo_credentials(monkeypatch):
    """Explicit dummy keys for tests whose exchange calls are replaced."""
    for name in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
        monkeypatch.setenv(name, "test-only-not-a-credential")


def pytest_collection_modifyitems(items):
    if os.environ.get("STONKFLY_FULL_TEST") == "1":
        return
    skip = pytest.mark.skip(reason="Full MaleCNS integration requires STONKFLY_FULL_TEST=1")
    for item in items:
        if item.get_closest_marker("full_graph"):
            item.add_marker(skip)


@pytest.fixture
def lightweight_controller(monkeypatch):
    """Use an explicit HOLD controller for execution-policy tests.

    Execution, ledger, guard and the CLI loop remain real. Neural behavior is
    tested separately against the full graph, never inferred from this double.
    """
    from stonkfly import data
    from stonkfly.neural.controller import FlyController

    def initialize(self, settings):
        self.s = settings
        self.brain = SimpleNamespace(circuit={"report": {}}, visual_report={})

    monkeypatch.setattr(data, "verify", lambda: {"test_double": True})
    monkeypatch.setattr(FlyController, "__init__", initialize)
    monkeypatch.setattr(FlyController, "observe", lambda self, frame, kind: {
        "side": "HOLD", "stimulus": kind, "memory": {"changed_edges": 0},
    })
    monkeypatch.setattr(FlyController, "save", lambda self, path: path.write_bytes(b"test brain"))
    monkeypatch.setattr(FlyController, "restore", lambda self, path: None)
