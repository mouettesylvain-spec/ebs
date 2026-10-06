from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.driver import Env, make_env


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)
