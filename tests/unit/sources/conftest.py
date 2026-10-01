"""Source tests store thousands of small blobs; CAS durability is tested in tests/unit/cas."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the CAS's per-object fsync, which dominates cold snapshots and is irrelevant here."""
    monkeypatch.setattr("ebs.cas.fs.os.fsync", lambda fd: None)
