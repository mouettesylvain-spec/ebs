"""A failed read inside bubblewrap comes back with a hint to run `ebs discover` (P3-01 R3)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.plan.keys import with_key
from ebs.runner.main import EXIT_OK
from tests.helpers.runner import Harness, shell_spec

pytestmark = pytest.mark.usefixtures("bwrap_available")


# R3
def test_hint(cas: FsCAS, tmp_path: Path) -> None:
    undeclared = tmp_path / "proj" / "defs.v"
    undeclared.parent.mkdir()
    undeclared.write_text("x\n")
    h = Harness(tmp_path, cas)
    spec = with_key(replace(shell_spec(f"cat {undeclared}", action_id="sim"), sandbox="bwrap"))
    code, build = h.run(spec)
    assert code == EXIT_OK
    manifest = h.result(build, "sim")
    assert manifest is not None
    assert manifest.status == "failed"
    hint = manifest.summary["sandbox_hint"]
    assert isinstance(hint, str)
    assert str(undeclared) in hint
    assert "ebs discover sim" in hint
    assert "ebs discover sim" in h.err.getvalue()
