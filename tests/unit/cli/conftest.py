"""CLI unit test fixtures (helpers: tests/helpers/cli.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.cli import Site, TestServices, write_site


@pytest.fixture
def site(tmp_path: Path) -> Site:
    write_site(tmp_path)
    return Site(tmp_path, TestServices(tmp_path))
