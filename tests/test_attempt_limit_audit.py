"""Every attempt-limit change -- including the first binding and any move to
unbounded (0) -- must land in an append-only audit trail inside the ledger."""

import pytest

from stonkfly.config import Settings
from stonkfly.ledger import Ledger


@pytest.fixture
def ledger(tmp_path):
    l = Ledger(tmp_path / "ledger.sqlite", Settings(), "paper")
    yield l
    l.close()


def test_first_binding_is_recorded(ledger):
    ledger.set_attempt_limit(24)
    trail = ledger.get("attempt_limit_changes")
    assert trail == [
        {"at": trail[0]["at"], "from": None, "to": 24,
         "attempts_used": 0, "allow_change": False}
    ]


def test_every_actual_change_is_appended(ledger):
    ledger.set_attempt_limit(24)
    ledger.set_attempt_limit(0, allow_change=True)
    trail = ledger.get("attempt_limit_changes")
    assert [e["from"] for e in trail] == [None, 24]
    assert [e["to"] for e in trail] == [24, 0]
    assert trail[1]["allow_change"] is True


def test_reaffirming_the_same_limit_records_nothing(ledger):
    ledger.set_attempt_limit(24)
    ledger.set_attempt_limit(24)  # every restart re-binds the same value
    assert len(ledger.get("attempt_limit_changes")) == 1


def test_change_without_acknowledgement_still_refuses(ledger):
    ledger.set_attempt_limit(24)
    with pytest.raises(RuntimeError, match="allow-attempt-limit-change"):
        ledger.set_attempt_limit(0)
    # The refused change left no trace.
    assert len(ledger.get("attempt_limit_changes")) == 1
