"""Invariant I18: the package layering from docs/design/overview.md holds (import-linter)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from importlinter.cli import lint_imports

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".importlinter"
SRC_PKG = ROOT / "src" / "ebs"


def _run_linter() -> int:
    return lint_imports(config_filename=str(CONFIG), no_cache=True)


@pytest.fixture
def ebs_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of src/ebs on tmp_path that import-linter analyses instead of the real one."""
    shutil.copytree(SRC_PKG, tmp_path / "ebs", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.syspath_prepend(str(tmp_path))
    # import-linter locates the package through importlib, which consults sys.modules first.
    for name in [n for n in sys.modules if n == "ebs" or n.startswith("ebs.")]:
        monkeypatch.delitem(sys.modules, name)
    return tmp_path / "ebs"


# R6
def test_contracts_hold() -> None:
    assert _run_linter() == 0


def test_copy_is_what_gets_analysed(ebs_copy: Path) -> None:
    # Guard for the violation tests: without a violation the copy is clean too.
    assert _run_linter() == 0


# R6
@pytest.mark.parametrize(
    ("module", "source", "contract"),
    [
        ("core/_violation.py", "import ebs.cli\n", "Package layering"),  # L0 -> L5
        ("plan/_violation.py", "from ebs import driver\n", "Package layering"),  # L2 -> L4
        ("runner/_violation.py", "import ebs.plan.planner\n", "runner does not import"),
        ("rules/_violation.py", "import ebs.cas\n", "rules import only"),
        ("toolchain/fingerprint.py", "import ebs.sources\n", "toolchain.fingerprint (L1)"),
        ("meta/_violation.py", "import ebs.exec\n", "meta store (L2)"),
        ("flow/_violation.py", "import ebs.plan\n", "Package layering"),  # L1 -> L2
        ("cas/_violation.py", "import ebs.sources\n", "Package layering"),  # L1 -> L2
        ("sources/_violation.py", "import ebs.plan\n", "Package layering"),  # plan drives sources
        ("rules/_violation.py", "import ebs.plan.types\n", "Package layering"),  # and rules
        ("core/_violation.py", "import ebs.meta\n", "core (L0)"),
        ("core/_violation.py", "import ebs.toolchain\n", "core (L0)"),
        ("flow/_violation.py", "import ebs.meta\n", "L0-L1 do not import meta"),
        ("plan/_violation.py", "import ebs.meta.service\n", "L0-L3 do not import meta.service"),
        ("toolchain/_violation.py", "import ebs.driver\n", "toolchain (at most L3)"),
        ("plan/_violation.py", "import ebs.toolchain.registry\n", "L0-L2 only use toolchain"),
        ("meta/service.py", "import ebs.cli\n", "Nothing imports cli"),
    ],
)
def test_violation_detected(
    ebs_copy: Path, capsys: pytest.CaptureFixture[str], module: str, source: str, contract: str
) -> None:
    # Modules from later tasks: contracts naming a missing module are ignored, so create them.
    for placeholder in ("plan/planner.py", "meta/service.py", "toolchain/registry.py"):
        (ebs_copy / placeholder).write_text('"""placeholder"""\n')
    (ebs_copy / module).write_text(source)
    assert _run_linter() != 0
    broken = [line for line in capsys.readouterr().out.splitlines() if line.endswith("BROKEN")]
    assert any(line.startswith(contract) for line in broken), broken
