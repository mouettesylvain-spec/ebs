from __future__ import annotations

import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path


def _ebs_executable() -> str:
    candidate = Path(sys.executable).parent / "ebs"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("ebs")
    assert found is not None, "the `ebs` console script is not installed (run `uv sync`)"
    return found


# R1
def test_version_matches_metadata() -> None:
    result = subprocess.run(
        [_ebs_executable(), "--version"], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ebs {version('ebs')}"
