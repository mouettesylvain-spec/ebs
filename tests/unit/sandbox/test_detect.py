"""Tests for sandbox selection (task P3-01 R1, open decision D6)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from ebs.core.errors import SandboxError
from ebs.flow.model import SandboxMode
from ebs.sandbox.detect import Detection, detect, run_probe

BWRAP = "/usr/bin/bwrap"
APPTAINER = "/usr/bin/apptainer"
IMAGE = "/images/os.sif"
USERNS_OFF = "bwrap: No permissions to create new namespace"


@dataclass
class Host:
    """A fake node: which binaries are on PATH and what each probe prints when it fails."""

    binaries: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)  # binary -> stderr of its probe
    probes: list[list[str]] = field(default_factory=list)

    def which(self, name: str) -> str | None:
        return self.binaries.get(name)

    def probe(self, argv: Sequence[str]) -> str | None:
        self.probes.append(list(argv))
        return self.failures.get(argv[0])


def _host(bwrap: str, apptainer: str) -> Host:
    """`bwrap`/`apptainer`: "ok", "broken" (on PATH but the probe fails) or "missing"."""
    host = Host()
    for name, path, state in (("bwrap", BWRAP, bwrap), ("apptainer", APPTAINER, apptainer)):
        if state != "missing":
            host.binaries[name] = path
        if state == "broken":
            host.failures[path] = USERNS_OFF if name == "bwrap" else "FATAL: no image"
    return host


def _detect(mode: SandboxMode, host: Host, image: str | None = IMAGE) -> Detection:
    return detect(mode, which=host.which, probe=host.probe, apptainer_image=image)


# R1: D6 default: bubblewrap, else Apptainer, else staging-only with a warning.
@pytest.mark.parametrize(
    ("mode", "bwrap", "apptainer", "image", "chosen", "warns"),
    [
        ("auto", "ok", "ok", IMAGE, "bwrap", False),
        ("auto", "missing", "ok", IMAGE, "apptainer", False),
        ("auto", "broken", "ok", IMAGE, "apptainer", False),
        ("auto", "broken", "missing", IMAGE, "staging", True),
        ("auto", "missing", "ok", None, "staging", True),  # no image configured
        ("auto", "missing", "broken", IMAGE, "staging", True),
        ("auto", "missing", "missing", IMAGE, "staging", True),
        ("bwrap", "ok", "missing", None, "bwrap", False),
        ("staging", "ok", "ok", IMAGE, "staging", False),
    ],
)
def test_selection(
    mode: SandboxMode, bwrap: str, apptainer: str, image: str | None, chosen: str, warns: bool
) -> None:
    found = _detect(mode, _host(bwrap, apptainer), image)
    assert found.sandbox.name == chosen
    assert (found.warning is not None) == warns
    assert found.reason  # always says why


# R1: the reason names what was missing, so `ebs status`/logs explain a staging-only run.
def test_reason_explains_fallback() -> None:
    found = _detect("auto", _host("broken", "missing"))
    assert found.warning is not None
    assert USERNS_OFF in found.reason
    assert "apptainer" in found.reason
    assert "staging" in found.warning


# R1: an explicit `bwrap` never silently degrades.
@pytest.mark.parametrize("state", ["missing", "broken"])
def test_explicit_bwrap_unavailable_raises(state: str) -> None:
    with pytest.raises(SandboxError, match="bwrap"):
        _detect("bwrap", _host(state, "ok"))


# R1: `staging` never probes the node.
def test_staging_does_not_probe() -> None:
    host = _host("ok", "ok")
    _detect("staging", host)
    assert host.probes == []


# R1: the bwrap probe uses the same namespaces as the real profile.
def test_bwrap_probe_argv() -> None:
    host = _host("ok", "missing")
    _detect("auto", host)
    probe = host.probes[0]
    assert probe[0] == BWRAP
    assert {"--unshare-all", "--share-net", "--die-with-parent"} <= set(probe)
    assert probe[-1] == "true"  # it starts a program in the namespaces, not just `--version`


# R1: the Apptainer probe runs a program from the configured image, so an unreadable image
# falls back instead of being chosen.
def test_apptainer_probe_argv() -> None:
    host = _host("missing", "ok")
    _detect("auto", host)
    probe = host.probes[-1]
    assert probe[:2] == [APPTAINER, "exec"]
    assert "--containall" in probe
    assert probe[-2:] == [IMAGE, "true"]


# R1: the real probe: success, failure with its stderr, and a program that does not exist.
def test_run_probe() -> None:
    assert run_probe(["/bin/sh", "-c", "exit 0"]) is None
    assert run_probe(["/bin/sh", "-c", "echo boom >&2; exit 3"]) == "boom"
    assert run_probe(["/bin/sh", "-c", "exit 4"]) == "exit code 4"
    missing = run_probe(["/nonexistent/bwrap"])
    assert isinstance(missing, str)
    assert missing
