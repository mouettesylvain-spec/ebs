"""A released build made from a committed flow, for sign-off bundle and `ebs verify` tests (P3-04).

`signed_off(tmp_path)` writes a CLI test site whose project is a git repository: a flow with a
toolchain (`.ebs/toolchains.yaml`, a pinned fingerprint) and an import `rtl` of the upstream
release `rtl-team/cpu 1` (a tree output whose build has a real plan in the CAS), pinned in a
committed `flow.lock`. It builds the flow with `ebs build` (scripted executor) and releases
`sim.result[*]` as `verif/cpu 2026.10.1`, whose bytes it puts in the CAS.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ebs.cas.fs import FsCAS
from ebs.cli._records import find_build
from ebs.core.digest import hash_bytes
from ebs.meta.api import ReleaseImport, ReleaseView
from ebs.plan.planfile import encode
from ebs.release.archive import Git
from ebs.release.releases import create_release
from tests.helpers.attest import sample_plan
from tests.helpers.cli import Site, TestServices, write_site
from tests.helpers.imports import store_output
from tests.helpers.release import Action, finish
from tests.helpers.sources import git, git_output

FLOW = """\
version: 1
project: verif
domain: test
imports:
  rtl: { from: rtl-team/cpu, channel: stable }
toolchains:
  fake: { module: fakesim/1.0 }
steps:
  lint:
    kind: shell
    toolchain: fake
    script: "cat imports/rtl/pack.srcs/rtl/*.sv src/a.txt > report.txt"
    inputs: { rtl: "${imports.rtl}/pack.srcs/**/*.sv", src: "src/*.txt" }
    outputs: { report: report.txt }
  sim:
    kind: shell
    matrix: { table: tests.csv }
    command: [cat, "${steps.lint.outputs.report}"]
    params: { test: "${row.test}" }
    outputs: { result: result.txt }
"""

TOOLCHAINS = f"""\
version: 1
toolchains:
  fakesim/1.0:
    fingerprint: "{hash_bytes(b"fakesim install tree")}"
    env: {{FAKESIM_MODE: batch}}
"""

BASE = {"lib/base.sv": "module base; endmodule\n"}
UPSTREAM = {"rtl/a.sv": "module a; endmodule\n", "rtl/sub/b.sv": "module b; endmodule\n"}


@dataclass
class SignedOff:
    site: Site
    release: ReleaseView
    upstream: ReleaseView
    cas: FsCAS

    @property
    def services(self) -> TestServices:
        return self.site.services


def hermetic_git() -> Git:
    """A `Git` running the real git with a hermetic config (no user or system files)."""
    return Git(git_output)


def publish_upstream(services: TestServices, tmp: Path) -> ReleaseView:
    """`rtl-team/cpu 1` on channel `stable`: a tree `pack.srcs`, made by a build whose plan is
    stored in the CAS (sign-off bundles carry the plans of imported releases) and which itself
    imported `lib/base 1` (a tree `gen.lib`: the second level of the lock closure)."""
    store = services.memory_store
    cas = services.cas("test")
    plan = cas.put_bytes(encode(sample_plan()))
    base = store_output(cas, tmp, BASE, deterministic=True)
    base_build = finish(store, {"gen": Action({"lib": base})}, project="base", plan_digest=plan)
    base_release = create_release(
        store, name="lib/base", version="1", build=base_build, outputs=["gen.lib"], by="bob"
    )
    srcs = store_output(cas, tmp, UPSTREAM, deterministic=True)
    build = finish(store, {"pack": Action({"srcs": srcs})}, project="rtl", plan_digest=plan,
                   status=None)  # fmt: skip
    store.record_release_imports(build, [ReleaseImport(alias="base", release=base_release.id)])
    store.finish_build(build, "passed")
    release = create_release(
        store, name="rtl-team/cpu", version="1", build=build, outputs=["pack.srcs"], by="bob"
    )
    store.set_channel("test", "rtl-team/cpu", "stable", release.id, by="bob")
    return release


def signed_off(tmp_path: Path) -> SignedOff:
    write_site(tmp_path, flow=FLOW)
    site = Site(tmp_path, TestServices(tmp_path))
    proj = site.proj
    (proj / ".ebs").mkdir(exist_ok=True)
    (proj / ".ebs" / "toolchains.yaml").write_text(TOOLCHAINS)
    upstream = publish_upstream(site.services, tmp_path / "upstream")
    locked = site.invoke(["lock", "update"])
    assert locked.exit_code == 0, locked.output
    git(proj, "init", "-q")
    git(proj, "add", "flow.yaml", "flow.lock", "tests.csv", "src", ".ebs/toolchains.yaml")
    git(proj, "commit", "-qm", "verif flow")
    built = site.invoke(["build"])
    assert built.exit_code == 0, built.output
    build = find_build(proj, None).build
    release = create_release(
        site.services.memory_store, name="verif/cpu", version="2026.10.1", build=build,
        outputs=["sim.result[*]"], by="alice",
    )  # fmt: skip
    cas = site.services.cas("test")
    for out in release.outputs.values():  # the scripted executor records digests only
        assert cas.put_bytes(f"{out.action_id}/{out.output}".encode()) == out.digest
    return SignedOff(site, release, upstream, cas)
