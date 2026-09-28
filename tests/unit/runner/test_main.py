from __future__ import annotations

import pytest

from ebs.runner.main import main


def test_runner_stub_exits_with_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "not implemented" in capsys.readouterr().err
