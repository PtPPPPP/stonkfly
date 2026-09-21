"""Promotion of a migrated ledger, and the checkpoint it depends on.

A ledger records a checkpoint; the run directory must actually hold that file,
unchanged, and the current ``FlyController`` must be able to load it. These tests
cover the gate, the verified copy, and the repair path that completes a prepared
migration without redoing it. All offline: the controller is injected, so no
connectome is loaded and no order is placed.
"""

import hashlib
import importlib.util
import json
import os

import pytest

from stonkfly.config import Settings
from stonkfly.ledger import Ledger

CHECKPOINT_BYTES = b"pretend neural state"


def _promotion_module():
    """Load the migration tool as a module so its functions can be tested."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "tools", "okx_ledger_migrate.py")
    spec = importlib.util.spec_from_file_location("okx_ledger_migrate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeBroker:
    def __init__(self, failure=None):
        self.failure = failure

    def verify_balances(self):
        if self.failure:
            raise self.failure


class _FakeController:
    def __init__(self, on_restore=None):
        self.path = None
        self.on_restore = on_restore

    def restore(self, path):
        if self.on_restore is not None:
            self.on_restore(path)
        self.path = path


def _controller_factory(controller=None, fail=False):
    def make():
        if fail:
            def boom(_path):
                raise ValueError("Checkpoint provenance mismatch")
            return _FakeController(on_restore=boom)
        return controller or _FakeController()
    return make


def _staged_view(tmp_path, **over):
    """A staged, self-consistent view with a real checkpoint file on disk."""
    name = over.pop("checkpoint_name", "brain-1.npz")
    content = over.pop("checkpoint_bytes", CHECKPOINT_BYTES)
    if content is not None:
        (tmp_path / name).write_bytes(content)
    checkpoint = {"file": name, "sha256": hashlib.sha256(CHECKPOINT_BYTES).hexdigest()}
    checkpoint.update(over.pop("checkpoint_extra", {}))
    if over.pop("drop_checkpoint", False):
        checkpoint = None
    meta = {
        "migration": {
            "state": "staged",
            "carried_orders": 2,
            "attempts_carried": 2,
            "gift_cross_check": [{"ccy": "USDT", "match": True}],
        },
        "baseline": {"USDT": "1000", "BTC": "1"},
        "budget": "100",
        "cash": "20",
        "positions": {"BTC-USDT": "0.001"},
        "identity": {"exchange": "okx", "environment": "okx-demo", "quote_ccy": "USDT"},
        "checkpoint": checkpoint,
    }
    meta.update(over)
    return {
        "path": str(tmp_path / "ledger.sqlite"),
        "meta": meta,
        "orders": [
            {"id": "a" * 32, "status": "SETTLED", "created": 1.0,
             "plan": {"product": "BTC-USDT"}, "exchange_id": "o1",
             "settlement": {"base": "0.001", "quote": "75", "fee": "0", "fee_ccy": "base"}},
            {"id": "b" * 32, "status": "REJECTED", "created": 2.0,
             "plan": {"product": "BTC-USDT"}, "exchange_id": None, "settlement": None},
        ],
    }


# ---------------------------------------------------------------------------
# Promotion preconditions.
# ---------------------------------------------------------------------------

def test_promotion_preconditions_hold_for_a_complete_staged_ledger(tmp_path):
    mod = _promotion_module()
    controller = _FakeController()
    view = _staged_view(tmp_path)
    assert mod.promotion_preconditions(
        view, _FakeBroker(), "100", _controller_factory(controller)
    ) == []
    # The restore was attempted against the resolved file in the run directory.
    assert controller.path == (tmp_path / "brain-1.npz").resolve()


@pytest.mark.parametrize(
    "field,value,needle",
    [
        ("migration", {"state": "promoted"}, "not 'staged'"),
        ("budget", None, "budget"),
        ("baseline", None, "baseline"),
        ("cash", None, "cash"),
        ("positions", None, "positions"),
    ],
)
def test_promotion_is_refused_when_the_artifact_is_incomplete(tmp_path, field, value, needle):
    mod = _promotion_module()
    view = _staged_view(tmp_path, **{field: value})
    problems = mod.promotion_preconditions(view, _FakeBroker(), "100", _controller_factory())
    assert problems and any(needle in p for p in problems)


def test_promotion_is_refused_when_recorded_counts_drift(tmp_path):
    mod = _promotion_module()
    view = _staged_view(tmp_path)
    view["meta"]["migration"]["carried_orders"] = 99
    problems = mod.promotion_preconditions(view, _FakeBroker(), "100", _controller_factory())
    assert any("carried_orders" in p for p in problems)


def test_promotion_is_refused_when_the_gift_cross_check_failed(tmp_path):
    mod = _promotion_module()
    view = _staged_view(tmp_path)
    view["meta"]["migration"]["gift_cross_check"] = [{"ccy": "BTC", "match": False}]
    problems = mod.promotion_preconditions(view, _FakeBroker(), "100", _controller_factory())
    assert any("cross-check failed" in p for p in problems)


def test_promotion_is_refused_on_a_budget_mismatch(tmp_path):
    mod = _promotion_module()
    view = _staged_view(tmp_path, budget="250")
    problems = mod.promotion_preconditions(view, _FakeBroker(), "100", _controller_factory())
    assert any("budget does not match" in p for p in problems)


def test_promotion_is_refused_while_an_intent_is_unresolved(tmp_path):
    mod = _promotion_module()
    view = _staged_view(tmp_path)
    view["orders"].append(
        {"id": "c" * 32, "status": "UNKNOWN", "created": 3.0,
         "plan": {"product": "BTC-USDT"}, "exchange_id": None, "settlement": None}
    )
    problems = mod.promotion_preconditions(view, _FakeBroker(), "100", _controller_factory())
    assert any("unresolved" in p for p in problems)


def test_promotion_is_refused_when_live_preconditions_fail(tmp_path):
    # Coverage, identity or balances failing right now must block promotion even
    # though the artifact itself is intact.
    mod = _promotion_module()
    view = _staged_view(tmp_path)
    problems = mod.promotion_preconditions(
        view, _FakeBroker(RuntimeError("x")), "100", _controller_factory()
    )
    assert problems == ["live preconditions failed: RuntimeError"]


def test_check_promotion_changes_nothing(tmp_path, capsys, demo_credentials):
    mod = _promotion_module()
    s = Settings(products=("BTC-USDT",))
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "okx-demo")
    ledger.put("order_attempts", 4)
    ledger.close()
    before = (tmp_path / "ledger.sqlite").read_bytes()

    assert mod.check_promotion(tmp_path) == 1
    out = capsys.readouterr().out
    assert "migration.state: None" in out
    assert "refused" in out
    assert (tmp_path / "ledger.sqlite").read_bytes() == before


# ---------------------------------------------------------------------------
# The checkpoint is neural state: present, legal, unchanged, loadable.
# ---------------------------------------------------------------------------

def test_promotion_requires_the_checkpoint_file_to_be_present(tmp_path):
    mod = _promotion_module()
    view = _staged_view(tmp_path)
    (tmp_path / "brain-1.npz").unlink()
    problems = mod.promotion_preconditions(view, _FakeBroker(), "100", _controller_factory())
    assert any("missing from the run directory" in p for p in problems)


def test_promotion_refuses_a_checkpoint_whose_hash_does_not_match(tmp_path):
    mod = _promotion_module()
    view = _staged_view(tmp_path)
    (tmp_path / "brain-1.npz").write_bytes(b"tampered or different state")
    problems = mod.promotion_preconditions(view, _FakeBroker(), "100", _controller_factory())
    assert any("does not match the ledger hash" in p for p in problems)


@pytest.mark.parametrize(
    "name",
    ["../evil.npz", "brain-1.npz.bak", "brain.npz", "/tmp/brain-1.npz", "brain-x.npz"],
)
def test_promotion_refuses_an_illegal_checkpoint_name(tmp_path, name):
    mod = _promotion_module()
    view = _staged_view(tmp_path, checkpoint_extra={"file": name})
    problems = mod.promotion_preconditions(view, _FakeBroker(), "100", _controller_factory())
    assert any("checkpoint path is not legal" in p for p in problems)


def test_promotion_refuses_a_checkpoint_the_controller_cannot_restore(tmp_path):
    # A matching hash proves the bytes are unchanged, not that the state is
    # compatible with the current graph, rule and configuration.
    mod = _promotion_module()
    view = _staged_view(tmp_path)
    problems = mod.promotion_preconditions(
        view, _FakeBroker(), "100", _controller_factory(fail=True)
    )
    assert any("cannot restore" in p for p in problems)


def test_promotion_refuses_when_no_checkpoint_is_recorded(tmp_path):
    mod = _promotion_module()
    view = _staged_view(tmp_path, drop_checkpoint=True)
    problems = mod.promotion_preconditions(view, _FakeBroker(), "100", _controller_factory())
    assert any("checkpoint metadata is missing" in p for p in problems)


def test_checkpoint_path_rejects_anything_but_a_plain_name(tmp_path):
    mod = _promotion_module()
    with pytest.raises(ValueError):
        mod.checkpoint_path(tmp_path, "../brain-1.npz")
    with pytest.raises(ValueError):
        mod.checkpoint_path(tmp_path, "notes.txt")
    assert mod.checkpoint_path(tmp_path, "brain-0.npz") == (tmp_path / "brain-0.npz").resolve()


def test_checkpoint_restorable_is_false_when_the_controller_refuses(tmp_path):
    mod = _promotion_module()
    path = tmp_path / "brain-1.npz"
    path.write_bytes(CHECKPOINT_BYTES)
    assert mod.checkpoint_restorable(path, _controller_factory()) is True
    assert mod.checkpoint_restorable(path, _controller_factory(fail=True)) is False


# ---------------------------------------------------------------------------
# The verified copy.
# ---------------------------------------------------------------------------

def test_copy_checkpoint_verifies_the_source_hash(tmp_path):
    mod = _promotion_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "brain-1.npz").write_bytes(b"state")
    recorded = hashlib.sha256(b"state").hexdigest()
    (source / "brain-1.npz").write_bytes(b"changed after the migration was prepared")
    with pytest.raises(ValueError, match="no longer matches the recorded hash"):
        mod.copy_checkpoint(
            {"path": str(source / "ledger.sqlite")}, target,
            {"file": "brain-1.npz", "sha256": recorded},
        )
    assert not (target / "brain-1.npz").exists()


def test_copy_checkpoint_refuses_to_overwrite_a_conflicting_file(tmp_path):
    mod = _promotion_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "brain-1.npz").write_bytes(b"state")
    recorded = hashlib.sha256(b"state").hexdigest()
    existing = target / "brain-1.npz"
    existing.write_bytes(b"a different, already present state")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        mod.copy_checkpoint(
            {"path": str(source / "ledger.sqlite")}, target,
            {"file": "brain-1.npz", "sha256": recorded},
        )
    # The conflicting file is left exactly as it was, and no partial remains.
    assert existing.read_bytes() == b"a different, already present state"
    assert not list(target.glob("*.partial"))


def test_copy_checkpoint_copies_and_verifies_the_target(tmp_path):
    mod = _promotion_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "brain-1.npz").write_bytes(CHECKPOINT_BYTES)
    record = mod.copy_checkpoint(
        {"path": str(source / "ledger.sqlite")}, target,
        {"file": "brain-1.npz", "sha256": hashlib.sha256(CHECKPOINT_BYTES).hexdigest()},
    )
    assert record["result"] == "copied"
    assert (target / "brain-1.npz").read_bytes() == CHECKPOINT_BYTES
    assert mod.sha256_file(target / "brain-1.npz") == record["sha256"]


def test_copy_checkpoint_is_a_no_op_when_the_target_already_matches(tmp_path):
    mod = _promotion_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    for d in (source, target):
        (d / "brain-1.npz").write_bytes(CHECKPOINT_BYTES)
    record = mod.copy_checkpoint(
        {"path": str(source / "ledger.sqlite")}, target,
        {"file": "brain-1.npz", "sha256": hashlib.sha256(CHECKPOINT_BYTES).hexdigest()},
    )
    assert record["result"] == "already_present"


def test_copy_checkpoint_refuses_a_missing_source_file(tmp_path):
    mod = _promotion_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    with pytest.raises(ValueError, match="has no brain-1.npz"):
        mod.copy_checkpoint(
            {"path": str(source / "ledger.sqlite")}, target,
            {"file": "brain-1.npz", "sha256": "irrelevant"},
        )


def test_a_copied_checkpoint_satisfies_the_promotion_gate(tmp_path):
    """The whole path: copy the recorded checkpoint, then the gate accepts it."""
    mod = _promotion_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "brain-1.npz").write_bytes(CHECKPOINT_BYTES)
    recorded = hashlib.sha256(CHECKPOINT_BYTES).hexdigest()
    mod.copy_checkpoint(
        {"path": str(source / "ledger.sqlite")}, target,
        {"file": "brain-1.npz", "sha256": recorded},
    )
    controller = _FakeController()
    problems = mod.promotion_preconditions(
        _staged_view(target), _FakeBroker(), "100", _controller_factory(controller)
    )
    assert problems == []
    assert controller.path == (target / "brain-1.npz").resolve()


# ---------------------------------------------------------------------------
# Repair: complete a prepared migration without redoing it.
# ---------------------------------------------------------------------------

def _staged_ledger(target, source_path, recorded, **extra):
    target.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(
        target / "ledger.sqlite", Settings(products=("BTC-USDT",)), "okx-demo",
        identity={"exchange": "okx", "environment": "okx-demo",
                  "account": "uid-1", "quote_ccy": "USDT"},
    )
    ledger.put("baseline", {"USDT": "1000", "BTC": "1"})
    ledger.put("budget", "100")
    ledger.put("cash", "20")
    ledger.put("positions", {"BTC-USDT": "0.001"})
    ledger.put("anchor", "99.5")
    ledger.put("order_attempts", 9)
    ledger.put("checkpoint", {"file": "brain-1.npz", "sha256": recorded})
    ledger.put("migration", {
        "state": "staged",
        "carried_orders": 1,
        "attempts_carried": 9,
        "checkpoint_chosen": {"path": str(source_path), "checkpoint": {"file": "brain-1.npz"}},
        **extra,
    })
    ledger.db.execute(
        "INSERT INTO orders(id,status,created,plan,exchange_id,settlement) VALUES (?,?,?,?,?,?)",
        ("c" * 32, "SETTLED", 1.0, json.dumps({"product": "BTC-USDT"}), "o1",
         json.dumps({"base": "0.001", "quote": "75", "fee": "0", "fee_ccy": "base"})),
    )
    return ledger


STATE_KEYS = ("baseline", "budget", "cash", "positions", "anchor",
              "order_attempts", "checkpoint", "migration")


def test_repair_checkpoint_writes_only_the_file_and_an_audit_entry(tmp_path, capsys):
    mod = _promotion_module()
    source = Ledger(tmp_path / "source" / "ledger.sqlite",
                    Settings(products=("BTC-USDT",)), "okx-demo")
    source.close()
    (tmp_path / "source" / "brain-1.npz").write_bytes(CHECKPOINT_BYTES)
    recorded = hashlib.sha256(CHECKPOINT_BYTES).hexdigest()
    target = tmp_path / "target"
    staged = _staged_ledger(target, source.path, recorded)
    before = {k: staged.get(k) for k in STATE_KEYS}
    staged.close()

    assert mod.repair_checkpoint(target) == 0
    assert (target / "brain-1.npz").read_bytes() == CHECKPOINT_BYTES

    after = Ledger(target / "ledger.sqlite", Settings(products=("BTC-USDT",)), "okx-demo")
    try:
        for key, value in before.items():
            assert after.get(key) == value, key
        assert after.attempts_used() == 9
        assert after.get("migration")["state"] == "staged"
        repairs = after.get("checkpoint_repairs")
        assert len(repairs) == 1
        assert repairs[0]["result"] == "copied"
        assert repairs[0]["sha256"] == recorded
        assert repairs[0]["file"] == "brain-1.npz"
    finally:
        after.close()
    assert "untouched" in capsys.readouterr().out


def test_repair_checkpoint_leaves_an_already_complete_directory_alone(tmp_path):
    mod = _promotion_module()
    source = Ledger(tmp_path / "source" / "ledger.sqlite",
                    Settings(products=("BTC-USDT",)), "okx-demo")
    source.close()
    (tmp_path / "source" / "brain-1.npz").write_bytes(CHECKPOINT_BYTES)
    recorded = hashlib.sha256(CHECKPOINT_BYTES).hexdigest()
    target = tmp_path / "target"
    staged = _staged_ledger(target, source.path, recorded)
    staged.close()
    (target / "brain-1.npz").write_bytes(CHECKPOINT_BYTES)

    assert mod.repair_checkpoint(target) == 0
    after = Ledger(target / "ledger.sqlite", Settings(products=("BTC-USDT",)), "okx-demo")
    try:
        assert after.get("checkpoint_repairs")[0]["result"] == "already_present"
    finally:
        after.close()


def test_repair_checkpoint_refuses_a_conflicting_existing_file(tmp_path, capsys):
    mod = _promotion_module()
    source = Ledger(tmp_path / "source" / "ledger.sqlite",
                    Settings(products=("BTC-USDT",)), "okx-demo")
    source.close()
    (tmp_path / "source" / "brain-1.npz").write_bytes(CHECKPOINT_BYTES)
    recorded = hashlib.sha256(CHECKPOINT_BYTES).hexdigest()
    target = tmp_path / "target"
    staged = _staged_ledger(target, source.path, recorded)
    staged.close()
    conflicting = b"some other neural state"
    (target / "brain-1.npz").write_bytes(conflicting)

    assert mod.repair_checkpoint(target) == 1
    assert (target / "brain-1.npz").read_bytes() == conflicting
    after = Ledger(target / "ledger.sqlite", Settings(products=("BTC-USDT",)), "okx-demo")
    try:
        assert after.get("checkpoint_repairs") is None
        assert after.get("migration")["state"] == "staged"
    finally:
        after.close()
    assert "refusing" in capsys.readouterr().out


def test_repair_checkpoint_refuses_when_the_source_no_longer_matches(tmp_path, capsys):
    mod = _promotion_module()
    source = Ledger(tmp_path / "source" / "ledger.sqlite",
                    Settings(products=("BTC-USDT",)), "okx-demo")
    source.close()
    recorded = hashlib.sha256(CHECKPOINT_BYTES).hexdigest()
    (tmp_path / "source" / "brain-1.npz").write_bytes(b"a different state now")
    target = tmp_path / "target"
    staged = _staged_ledger(target, source.path, recorded)
    staged.close()

    assert mod.repair_checkpoint(target) == 1
    assert not (target / "brain-1.npz").exists()
    after = Ledger(target / "ledger.sqlite", Settings(products=("BTC-USDT",)), "okx-demo")
    try:
        assert after.get("checkpoint_repairs") is None
        assert after.get("migration")["state"] == "staged"
        assert after.attempts_used() == 9
    finally:
        after.close()
