"""Real bubblewrap for the sandbox integration tests (task P3-01 R3).

Hosts without bwrap or unprivileged user namespaces (most CI containers) skip these tests; the
grid nightly sets `EBS_REQUIRE_SANDBOX=1`, which turns the skip into a failure there.
"""

from __future__ import annotations

import os

import pytest

from ebs.core.errors import SandboxError
from ebs.sandbox.detect import detect


@pytest.fixture
def bwrap_available() -> None:
    try:
        detect("bwrap")
    except SandboxError as exc:
        if os.environ.get("EBS_REQUIRE_SANDBOX") == "1":
            pytest.fail(f"EBS_REQUIRE_SANDBOX=1 but bubblewrap cannot run here: {exc}")
        pytest.skip(
            f"bubblewrap cannot run on this host (set EBS_REQUIRE_SANDBOX=1 to fail): {exc}"
        )
