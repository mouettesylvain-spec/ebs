from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fakes.slurm import FakeSlurm
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url

__all__ = ["_pg_truncate_engine", "fake_slurm", "pg_migrated", "pg_server", "pg_url"]


@pytest.fixture
def fake_slurm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSlurm]:
    """FakeSlurm with its commands in a temp bin dir; kills leftover jobs at teardown.

    The cwd moves to tmp_path: sbatch writes default `slurm-%j.out` logs to the submit dir.
    """
    monkeypatch.chdir(tmp_path)
    fake = FakeSlurm.install(tmp_path / "fakeslurm")
    yield fake
    fake.shutdown()
