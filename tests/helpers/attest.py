"""A released build with a stored plan, an import and a toolchain, for attestation tests (P3-03).

`released(cas)` records in a fresh in-memory store: an upstream release `rtl-team/cpu 1` (the
import `rtl`), and a build of `demo` whose plan (in `cas`) uses a toolchain and a lock, then
releases its `lint.report` file and `pack.srcs` tree as `verif/cpu 2026.10.1`. The released
bytes are in `cas`: a file and a directory tree.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest, hash_bytes, hash_file
from ebs.core.tree import build_tree
from ebs.meta.api import BuildView, OutputResult, ReleaseImport, ReleaseView
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.planfile import encode
from ebs.plan.types import FlowInfo, GitInfo, Plan, PlanToolchain, RuleRef, ToolchainRef
from ebs.release.releases import create_release
from tests.helpers.plan import sample_spec
from tests.helpers.release import Action, finish, out

QUESTA_ID = hash_bytes(b"toolchain:questa/2025.2")
LOCK = hash_bytes(b"lock")
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@dataclass
class Released:
    store: InMemoryMetadataStore
    release: ReleaseView
    build: BuildView
    plan: Plan
    upstream: ReleaseView
    report: Path  # the released file
    tree: Path  # the released directory


def sample_plan() -> Plan:
    tc = ToolchainRef("questa", "questa/2025.2", QUESTA_ID)
    return Plan(
        ebs_version="1.2.3",
        domain="test",
        project="demo",
        flow=FlowInfo("flows/cpu/flow.yaml", GitInfo("git@example.invalid:cpu.git", COMMIT, False)),
        toolchains={"questa": PlanToolchain("questa/2025.2", QUESTA_ID)},
        actions=(
            sample_spec(
                action_id="lint",
                step="lint",
                rule=RuleRef("shell", "1"),
                params={"top": "cpu", "n": 2},
                toolchain=tc,
                domain="test",
            ),
            sample_spec(
                action_id="pack",
                step="pack",
                rule=RuleRef("shell", "1"),
                params={"opt": "-O2"},
                toolchain=None,
                domain="test",
            ),
        ),
        edges=(),
        lock=LOCK,
    )


def released(cas: FsCAS, work: Path, store: InMemoryMetadataStore | None = None) -> Released:
    store = store or InMemoryMetadataStore()
    up_build = finish(store, {"pack": Action({"srcs": out(b"rtl", kind="tree")})})
    upstream = create_release(
        store, name="rtl-team/cpu", version="1", build=up_build, outputs=["pack.srcs"], by="bob"
    )

    work.mkdir(parents=True, exist_ok=True)
    report = work / "report.txt"
    report.write_bytes(b"0 violations\n")
    tree = work / "srcs"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.sv").write_text("module a; endmodule\n")
    (tree / "sub" / "b.sv").write_text("module b; endmodule\n")
    report_digest = cas.put_bytes(report.read_bytes())
    tree_digest, _ = build_tree(tree, hash_file)

    plan = sample_plan()
    plan_digest = cas.put_bytes(encode(plan))
    build_id = finish(
        store,
        {
            "lint": Action(
                {
                    "report": OutputResult(
                        digest=report_digest, id=report_digest, type="file", size=13
                    )
                }
            ),
            "pack": Action(
                {
                    "srcs": OutputResult(
                        digest=tree_digest, id=hash_bytes(b"nd"), type="tree", size=40
                    )
                }
            ),
        },
        flow_repo="git@example.invalid:cpu.git",
        flow_commit=COMMIT,
        plan_digest=plan_digest,
    )
    store.record_release_imports(build_id, [ReleaseImport(alias="rtl", release=upstream.id)])
    release = create_release(
        store,
        name="verif/cpu",
        version="2026.10.1",
        build=build_id,
        outputs=["lint.report", "pack.srcs"],
        by="alice",
    )
    return Released(store, release, store.get_build(build_id), plan, upstream, report, tree)


def tree_digest(path: Path) -> Digest:
    return build_tree(path, hash_file)[0]


@dataclass
class TeamKey:
    key: Path  # private key (what `[release.signing].key` names)
    allowed_signers: Path
    principal: str


def ssh_keygen_or_skip() -> str:
    found = shutil.which("ssh-keygen")
    if found is None:
        pytest.skip("missing dependency: ssh-keygen (OpenSSH >= 8.2, package openssh-client)")
    return found


def team_key(base: Path, principal: str = "cpu-team@example.invalid") -> TeamKey:
    """A fresh ed25519 key without passphrase and an allowed-signers file trusting it."""
    program = ssh_keygen_or_skip()
    base.mkdir(parents=True, exist_ok=True)
    key = base / "team_ed25519"
    subprocess.run(
        [program, "-q", "-t", "ed25519", "-N", "", "-C", principal, "-f", str(key)],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    public = (base / "team_ed25519.pub").read_text().split()
    allowed = base / "allowed_signers"
    allowed.write_text(f'{principal} namespaces="ebs-release" {public[0]} {public[1]}\n')
    return TeamKey(key, allowed, principal)
