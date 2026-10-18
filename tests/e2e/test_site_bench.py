"""scripts/site_bench.py on real builds (task D-01 R8): its parser reads what `ebs build --json`
actually prints, and the scenario story holds (a fresh checkout is all cache hits)."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy.engine import URL

from tests.helpers.e2e import EXAMPLES, REPO_ROOT, Site
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

SCRIPT = REPO_ROOT / "scripts" / "site_bench.py"


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("site_bench", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["site_bench"] = module
    spec.loader.exec_module(module)
    return module


# R8
def test_parse_build_on_real_output(bench: ModuleType, tmp_path: Path, pg_url: URL) -> None:
    site = Site(tmp_path / "site", pg_url)

    def build(checkout: str) -> object:
        work = tmp_path / checkout
        if not work.exists():
            shutil.copytree(EXAMPLES / "lint-make", work, ignore=shutil.ignore_patterns(".ebs"))
        proc = site.ebs(work, "build", "--cache", "write", "--json")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return bench.parse_build(proc.stdout)

    assert build("first") == bench.BuildCounts(executed=3, cached=0, total=3)
    assert build("fresh-clone") == bench.BuildCounts(executed=0, cached=3, total=3)
    bench.SCENARIOS[2].edit(tmp_path / "fresh-clone")  # the comment edit of lint-comment
    assert build("fresh-clone") == bench.BuildCounts(executed=1, cached=2, total=3)
