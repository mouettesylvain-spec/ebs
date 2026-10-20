"""The runner runs the tool through the action's sandbox (task P3-01 R1, R3, R5).

A fake sandbox runs the command unwrapped and records what the runner asked for, so these tests
need no bubblewrap; tests/integration/sandbox runs the real one.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.core.errors import SandboxError
from ebs.domains import DomainConfig, Domains
from ebs.flow.model import SandboxMode
from ebs.plan.keys import with_key
from ebs.plan.types import ActionSpec, PlanToolchain, ToolchainRef
from ebs.runner.main import EXIT_INFRA, EXIT_OK, EXIT_USAGE
from ebs.sandbox.api import Mount, Sandbox
from ebs.sandbox.apptainer import ApptainerSandbox
from ebs.sandbox.detect import Detection
from ebs.sandbox.staging import StagingSandbox
from tests.helpers.runner import DOMAIN, Harness, shell_spec, source_file

TC_ID = hash_bytes(b"tc")
TOOLCHAINS = {"questa": PlanToolchain("questa/2025.2", TC_ID, {}, ("/eda/questa", "/eda/uvm"))}


@dataclass
class FakeSandbox:
    """Runs argv as is; `setup` makes it report a setup failure like bwrap's own errors."""

    name: str = "bwrap"
    setup: str | None = None
    calls: list[tuple[list[str], list[Mount], dict[str, str], str]] = field(default_factory=list)
    setup_calls: list[tuple[int, str]] = field(default_factory=list)

    def wrap(
        self, argv: Sequence[str], mounts: Sequence[Mount], env: Mapping[str, str], cwd: str
    ) -> list[str]:
        self.calls.append((list(argv), list(mounts), dict(env), cwd))
        return list(argv)

    def setup_error(self, exit_code: int, log_head: str) -> str | None:
        self.setup_calls.append((exit_code, log_head))
        return self.setup


@dataclass
class Detector:
    sandbox: Sandbox
    warning: str | None = None
    modes: list[SandboxMode] = field(default_factory=list)
    error: str | None = None

    def __call__(self, mode: SandboxMode) -> Detection:
        self.modes.append(mode)
        if self.error is not None:
            raise SandboxError(self.error)
        return Detection(self.sandbox, "test", self.warning)


def _spec(script: str, mode: SandboxMode = "auto", **kw: object) -> ActionSpec:
    return with_key(replace(shell_spec(script, **kw), sandbox=mode))  # type: ignore[arg-type]


def _harness(tmp_path: Path, cas: FsCAS, detector: Detector, **overrides: object) -> Harness:
    return Harness(
        tmp_path, cas, settings_overrides={"sandbox_ro_binds": ("/site/tools",), **overrides},
        sandboxes=detector,
    )  # fmt: skip


# R5: the sandbox used is recorded in the manifest; the key is the plan's, sandbox or not.
def test_manifest_records_sandbox(cas: FsCAS, tmp_path: Path) -> None:
    detector = Detector(FakeSandbox())
    h = _harness(tmp_path, cas, detector)
    spec = _spec("exit 0", mode="auto")
    code, build = h.run(spec)
    assert code == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.runner.sandbox == "bwrap"
    assert manifest.action_key == spec.key == shell_spec("exit 0").key
    assert detector.modes == ["auto"]


# R5: a staging-only run says so too.
def test_manifest_records_staging(cas: FsCAS, tmp_path: Path) -> None:
    h = _harness(tmp_path, cas, Detector(StagingSandbox()))
    code, build = h.run(_spec("exit 0", mode="staging"))
    assert code == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.runner.sandbox == "staging"


# R2: what the runner exposes: toolchain roots, site binds and the domain's CAS read-only,
# the action's scratch read-write; the tool starts in work/ with the scrubbed env.
def test_mounts(cas: FsCAS, tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    h = _harness(tmp_path, cas, Detector(sandbox))
    tc = ToolchainRef("questa", "questa/2025.2", TC_ID)
    spec = _spec("exit 0", toolchain=tc, inputs=[source_file(cas, "a.v", b"x")])
    code, _ = h.run(spec, toolchains=TOOLCHAINS)
    assert code == EXIT_OK
    ((argv, mounts, env, cwd),) = sandbox.calls
    assert argv == list(spec.argv)
    ro = [m.src for m in mounts if not m.writable]
    rw = [m.src for m in mounts if m.writable]
    assert ro == ["/eda/questa", "/eda/uvm", "/site/tools", str(tmp_path / "cas" / "test")]
    assert len(rw) == 1
    assert cwd == str(Path(rw[0]) / "work")
    assert env["HOME"] == str(Path(rw[0]) / "home")
    assert all(m.src == m.dst for m in mounts)  # paths inside equal paths outside
    # Scratch comes last: in bwrap a later mount covers an earlier one, so a read-only parent
    # (e.g. a ro_bind of the scratch filesystem) can never make scratch read-only.
    assert mounts[-1].writable
    assert not any(m.writable for m in mounts[:-1])


# R2: a large input is a link into its domain's CAS, which may have its own root ([[domains]]).
def test_mounts_use_domain_cas_root(cas: FsCAS, tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    domain_root = tmp_path / "domain-cas"
    domains = Domains({DOMAIN: DomainConfig(DOMAIN, "grp", cas_root=domain_root)})
    h = _harness(tmp_path, cas, Detector(sandbox), domains=domains)
    code, _ = h.run(_spec("exit 0"))
    assert code == EXIT_OK
    ro = [m.src for m in sandbox.calls[0][1] if not m.writable]
    assert str(domain_root / DOMAIN) in ro
    assert str(tmp_path / "cas" / DOMAIN) not in ro


# R2: a root named twice (toolchain and site bind) is mounted once.
def test_mounts_deduplicated(cas: FsCAS, tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    h = _harness(tmp_path, cas, Detector(sandbox), sandbox_ro_binds=("/eda/questa",))
    tc = ToolchainRef("questa", "questa/2025.2", TC_ID)
    code, _ = h.run(_spec("exit 0", toolchain=tc), toolchains=TOOLCHAINS)
    assert code == EXIT_OK
    ro = [m.src for m in sandbox.calls[0][1] if not m.writable]
    assert len(ro) == len(set(ro))
    assert ro.count("/eda/questa") == 1


# R1: the runner hands the sandbox the tool's real exit code and the *head* of its log.
def test_setup_error_sees_exit_code_and_log_head(cas: FsCAS, tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    h = _harness(tmp_path, cas, Detector(sandbox))
    script = "echo 'bwrap: Can'\\''t find source path /x' >&2; seq 1 20000; exit 1"
    code, _ = h.run(_spec(script))
    assert code == EXIT_OK
    ((exit_code, head),) = sandbox.setup_calls
    assert exit_code == 1
    assert head.startswith("bwrap: Can't find source path /x\n")


# R1: the default detector uses the configured Apptainer image.
def test_default_detector_uses_configured_image(cas: FsCAS, tmp_path: Path) -> None:
    runner = Harness(
        tmp_path, cas, settings_overrides={"sandbox_apptainer_image": "/img/os.sif"}
    ).runner()
    detector = runner._sandboxes
    assert isinstance(detector, functools.partial)
    assert detector.keywords["apptainer_image"] == "/img/os.sif"


# R3: a failed run under a sandbox gets a hint pointing at `ebs discover`.
def test_hint_in_summary_and_stderr(cas: FsCAS, tmp_path: Path) -> None:
    h = _harness(tmp_path, cas, Detector(FakeSandbox()))
    missing = tmp_path / "proj" / "defs.v"
    code, build = h.run(_spec(f"cat {missing}"))
    assert code == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == "failed"
    hint = manifest.summary["sandbox_hint"]
    assert isinstance(hint, str)
    assert str(missing) in hint
    assert "ebs discover s" in hint
    assert "ebs discover s" in h.err.getvalue()


# R3: no hint when the tool passed, or ran without a real sandbox.
def test_no_hint(cas: FsCAS, tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    passed = f"cat {missing} 2>&1 || true; exit 0"  # a not-found message, yet the tool passed
    for sandbox, script in ((FakeSandbox(), passed), (StagingSandbox(), f"cat {missing}")):
        h = _harness(tmp_path, cas, Detector(sandbox))
        _, build = h.run(_spec(script))
        manifest = h.result(build)
        assert manifest is not None
        assert "sandbox_hint" not in manifest.summary


# R1: a sandbox that could not start the tool is an infra failure (retried), never a cached
# FAILED result.
def test_setup_failure_is_infra(cas: FsCAS, tmp_path: Path) -> None:
    h = _harness(tmp_path, cas, Detector(FakeSandbox(setup="bwrap: Can't find source path")))
    code, build = h.run(_spec("exit 1"))
    assert code == EXIT_INFRA
    assert h.result(build) is None
    assert h.store.cache_puts == []
    assert "Can't find source path" in h.err.getvalue()
    assert h.scratch_entries() == []  # I16


# R1: an explicit `bwrap` the node cannot provide is an infra failure; nothing runs.
def test_unavailable_is_infra(cas: FsCAS, tmp_path: Path) -> None:
    h = _harness(tmp_path, cas, Detector(FakeSandbox(), error="bwrap is not installed"))
    code, build = h.run(_spec(f"touch {tmp_path / 'ran'}", mode="bwrap"))
    assert code == EXIT_INFRA
    assert h.result(build) is None
    assert not (tmp_path / "ran").exists()
    assert "bwrap is not installed" in h.err.getvalue()
    assert h.scratch_entries() == []


# R1 (D6): falling back to staging-only warns on stderr and still runs.
def test_fallback_warning(cas: FsCAS, tmp_path: Path) -> None:
    warning = "no sandbox available on this node; running with staging only"
    h = _harness(tmp_path, cas, Detector(StagingSandbox(), warning=warning))
    code, _ = h.run(_spec("exit 0"))
    assert code == EXIT_OK
    assert warning in h.err.getvalue()


# R1: a mount the sandbox cannot express is a configuration error (exit 64), not an ebs bug.
def test_unexpressible_mount_is_usage_error(cas: FsCAS, tmp_path: Path) -> None:
    sandbox = ApptainerSandbox("/usr/bin/apptainer", "/images/os.sif")
    h = _harness(tmp_path, cas, Detector(sandbox), sandbox_ro_binds=("/site/a:b",))
    code, build = h.run(_spec(f"touch {tmp_path / 'ran'}"))
    assert code == EXIT_USAGE
    assert h.result(build) is None
    assert not (tmp_path / "ran").exists()
    assert "':'" in h.err.getvalue()
    assert h.scratch_entries() == []  # I16
