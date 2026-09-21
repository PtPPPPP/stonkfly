"""Checkpoint durability: fsync'd atomic writes and strict committed-state
verification. All offline; the brain is a minimal stand-in with the same
checkpoint surface."""

import hashlib
import json
import os

import numpy as np
import pytest

from stonkfly.config import Settings
from stonkfly.ledger import Ledger
from stonkfly.neural import brain as brain_mod
from stonkfly.neural.brain import MemoryBrain, restore_verified


class FakeBrain:
    """Just the attributes ``Brain.checkpoint`` touches."""

    fields: tuple = ()
    weight = np.array([0.5], dtype=np.float64)
    ids = np.array([1], dtype=np.int32)
    ptr = np.array([0, 1], dtype=np.int32)
    post = np.array([1], dtype=np.int32)
    circuit = {"edges": np.array([1], dtype=np.int32)}
    build = "test-build"
    eta = 0.5
    cursor = 7
    weights_frozen = False
    total_spikes = 3

    def configuration_signature(self):
        return {"k": "v"}

    checkpoint = MemoryBrain.checkpoint


class RecordingController:
    def __init__(self):
        self.restored = []

    def restore(self, path):
        self.restored.append(str(path))


def test_checkpoint_is_fsynced_before_replace(tmp_path, monkeypatch):
    calls = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr("stonkfly.neural.brain.os.fsync", recording_fsync)
    brain = FakeBrain()
    path = tmp_path / "brain-0.npz"
    brain.checkpoint(path)

    assert path.exists()
    assert not path.with_suffix(".partial").exists()
    assert calls, "checkpoint never fsynced the data blocks"
    with np.load(path, allow_pickle=False) as a:
        assert float(a["weight"][0]) == 0.5


def test_directory_fsync_is_best_effort_and_silent_where_unsupported(tmp_path, monkeypatch):
    # On Windows there is no directory fsync; the helper must detect the
    # platform and never raise. Force the POSIX branch to prove the OSError
    # path is swallowed (os.open on a directory fails on Windows anyway).
    monkeypatch.setattr("stonkfly.neural.brain.os.name", "posix")
    brain_mod._fsync_directory(tmp_path)  # must not raise
    monkeypatch.setattr("stonkfly.neural.brain.os.name", "nt")
    brain_mod._fsync_directory(tmp_path)  # no-op on nt


def _seed(out, ledger, primary_bytes, prev_bytes=None):
    primary = out / "brain-0.npz"
    primary.write_bytes(primary_bytes)
    info = {"file": "brain-0.npz", "sha256": hashlib.sha256(primary_bytes).hexdigest()}
    if prev_bytes is not None:
        (out / "brain-1.npz").write_bytes(prev_bytes)
        info["prev_file"] = "brain-1.npz"
        info["prev_sha256"] = hashlib.sha256(prev_bytes).hexdigest()
    ledger.put("checkpoint", info)
    return info


def test_restore_verified_happy_path(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", Settings(), "paper")
    try:
        controller = RecordingController()
        info = _seed(tmp_path, ledger, b"primary-state")
        used = restore_verified(controller, tmp_path, info, ledger=ledger)
        assert used == "brain-0.npz"
        assert controller.restored == [str(tmp_path / "brain-0.npz")]
        assert ledger.get("checkpoint_fallbacks") is None
    finally:
        ledger.close()


def test_corrupt_primary_refuses_even_when_previous_slot_is_valid(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", Settings(), "paper")
    try:
        controller = RecordingController()
        info = _seed(tmp_path, ledger, b"torn-primary", prev_bytes=b"good-prev")
        # Simulate the power-loss window: the committed file no longer matches
        # the recorded hash, but the previous slot does.
        (tmp_path / "brain-0.npz").write_bytes(b"corrupted-after-commit")
        with pytest.raises(RuntimeError, match="Checkpoint integrity mismatch"):
            restore_verified(controller, tmp_path, info, ledger=ledger)
        assert controller.restored == []
        assert ledger.get("checkpoint") == info
        assert ledger.get("checkpoint_fallbacks") is None
    finally:
        ledger.close()


def test_integrity_mismatch_still_raises_without_a_valid_fallback(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", Settings(), "paper")
    try:
        controller = RecordingController()
        info = _seed(tmp_path, ledger, b"primary", prev_bytes=b"prev")
        (tmp_path / "brain-0.npz").write_bytes(b"corrupted")
        (tmp_path / "brain-1.npz").write_bytes(b"also-corrupted")
        with pytest.raises(RuntimeError, match="Checkpoint integrity mismatch"):
            restore_verified(controller, tmp_path, info, ledger=ledger)
        assert controller.restored == []
    finally:
        ledger.close()


def test_integrity_mismatch_raises_when_no_previous_slot_was_recorded(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", Settings(), "paper")
    try:
        controller = RecordingController()
        info = _seed(tmp_path, ledger, b"primary")
        (tmp_path / "brain-0.npz").write_bytes(b"corrupted")
        with pytest.raises(RuntimeError, match="Checkpoint integrity mismatch"):
            restore_verified(controller, tmp_path, info, ledger=ledger)
    finally:
        ledger.close()
