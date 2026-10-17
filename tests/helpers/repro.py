"""A finished two-action build run by the real runner, for `ebs debug` / `ebs reproduce` (P1-10).

- `gen` (step gen): passed. Input `src/a.txt` (source), declared env `FOO=bar`, toolchain `tc`;
  outputs `out` (file `out.txt`, deterministic) and `model` (dir, `deterministic: false`).
- `sim[test=smoke]` (step sim): failed (the tool exits 1). Inputs `out.txt` (gen's `out`) and
  `model` (gen's nondeterministic `model`, resolved through the store), declared env `SEED=7`,
  resources 2 cpus / 3 GiB / 600 s, license `simfeat: 1`. Its debug dir holds `command.json`.

Both actions ran through `ebs.runner.main.Runner` with the site's own runner settings and the
CLI's caller env, with `--keep-scratch`, so tests can compare a reproduced sandbox with the
runner's real one. The build record names the final plan; events give `sim` job id 4242.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ebs.cas.fs import FsCAS
from ebs.cli._records import BuildRecord
from ebs.core.digest import hash_bytes
from ebs.flow.model import Resources
from ebs.meta.api import ActionRow, BuildCreate, BuildId, ResultManifest
from ebs.plan.keys import with_key
from ebs.plan.planfile import encode
from ebs.plan.types import (
    ActionOutputInput,
    ActionSpec,
    FlowInfo,
    InputRef,
    OutputSpec,
    Plan,
    PlanToolchain,
    ToolchainRef,
)
from ebs.rules.registry import RuleRegistry
from ebs.rules.shell import ShellRule
from ebs.runner.main import EXIT_OK, Runner, RunRequest
from ebs.runner.stage import action_dir_name
from tests.helpers.cli import DOMAIN, Site, write_build
from tests.helpers.runner import shell_spec, source_file

U2 = UUID("22222222-2222-4222-8222-222222222222")
GEN = "gen"
SIM = "sim[test=smoke]"
NODE = "node-7"
JOB = "4242"
LICENSE_ENV = {"LM_LICENSE_FILE": "1717@license.example"}  # passthrough, never in the key
TOOLCHAIN = PlanToolchain(
    module="demo-tool/1.0",
    id=hash_bytes(b"toolchain demo-tool/1.0"),
    env={"TOOL_HOME": "$HOME/tool", "PATH": "/usr/bin:/bin"},
)
GEN_SCRIPT = "cat src/a.txt > out.txt\nmkdir -p model/sub\nprintf 'module m;\\n' > model/sub/m.v\n"
SIM_SCRIPT = "echo 'Error: assertion failed at top.sv:12'\nexit 1\n"
FIRST_ERROR = "Error: assertion failed at top.sv:12"
SIGNATURE = "Error: assertion failed at <path>:<n>"


@dataclass(frozen=True)
class ReproBuild:
    build: int
    uuid: UUID
    cas: FsCAS
    plan: Plan
    results: dict[str, ResultManifest]
    kept: dict[str, Path]  # action id -> the runner's kept scratch root
    debug_root: Path

    def spec(self, action_id: str) -> ActionSpec:
        return self.plan.action(action_id)


def write_repro_site(site: Site, *, copy_threshold: int | None = None, extra: str = "") -> None:
    """Add `[debug].root` (and optionally `[cas].copy_threshold`) to the site config."""
    config = site.root / "config.toml"
    text = config.read_text()
    if copy_threshold is not None:
        text = text.replace("[cas]\n", f"[cas]\ncopy_threshold = {copy_threshold}\n", 1)
    config.write_text(text + f'[debug]\nroot = "{site.root / "debug"}"\n' + extra)
    (site.root / "debug" / DOMAIN).mkdir(parents=True, exist_ok=True)  # the admin's domain dir
    site.services._config = None  # re-read


def _gen(cas: FsCAS) -> ActionSpec:
    return shell_spec(
        GEN_SCRIPT,
        action_id=GEN,
        inputs=[source_file(cas, "src/a.txt", b"hello\n")],
        outputs=[
            OutputSpec("model", "model", "dir", deterministic=False),
            OutputSpec("out", "out.txt", "file"),
        ],
        env={"FOO": "bar"},
        toolchain=ToolchainRef("tc", TOOLCHAIN.module, TOOLCHAIN.id),
    )


def _sim(gen: ActionSpec, gen_result: ResultManifest | None) -> ActionSpec:
    out_id = None if gen_result is None else gen_result.outputs["out"].id
    model_id = None if gen_result is None else gen_result.outputs["model"].id
    spec = shell_spec(
        SIM_SCRIPT,
        action_id=SIM,
        inputs=[
            InputRef("model", "tree", ActionOutputInput(GEN, "model"), model_id),
            InputRef("out.txt", "file", ActionOutputInput(GEN, "out"), out_id),
        ],
        outputs=[OutputSpec("result", "result.txt", "file", optional=True)],
        env={"SEED": "7"},
    )
    spec = dataclasses.replace(
        spec,
        resources=Resources(cpus=2, mem="3G", time="600s"),
        licenses={"simfeat": 1},
        key=None,
    )
    return with_key(spec)


def _plan(*specs: ActionSpec) -> Plan:
    return Plan(
        ebs_version="test",
        domain=DOMAIN,
        project="demo",
        flow=FlowInfo(),
        toolchains={"tc": TOOLCHAIN},
        actions=specs,
        edges=(),
    )


def write_repro_build(site: Site) -> ReproBuild:
    """Run `gen` then `sim` through the real runner and record the build (module docstring)."""
    services = site.services
    services.environ.update(LICENSE_ENV)
    cas = services.cas(DOMAIN)
    store = services.memory_store
    gen = _gen(cas)
    build = store.create_build(
        BuildCreate(
            uuid=U2, domain=DOMAIN, project="demo", plan_digest=hash_bytes(b"submitted"),
            flow_repo="", flow_commit="", flow_dirty=False, user_name="alice", cache_mode="write",
        )
    )  # fmt: skip
    store.add_actions(
        build, [ActionRow(action_id=GEN, step="gen"), ActionRow(action_id=SIM, step="sim")]
    )
    runner = Runner(
        cas=cas,
        store=store,
        rules=RuleRegistry([ShellRule()]),
        settings=services.runner_settings(),
        caller_env=services.environ,
        host=NODE,
        clock=services.clock,
    )
    first = cas.put_bytes(encode(_plan(gen, _sim(gen, None))))
    assert runner.run(RunRequest(first, GEN, BuildId(build), keep_scratch=True)) == EXIT_OK
    gen_result = store.get_result(BuildId(build), GEN)
    assert gen_result is not None
    assert gen_result.status == "passed"
    plan = _plan(gen, _sim(gen, gen_result))
    final = cas.put_bytes(encode(plan))
    assert runner.run(RunRequest(final, SIM, BuildId(build), keep_scratch=True)) == EXIT_OK
    sim_result = store.get_result(BuildId(build), SIM)
    assert sim_result is not None
    assert sim_result.status == "failed"
    scratch = services.runner_settings().scratch_dir / str(build)
    kept = {a: scratch / action_dir_name(a) for a in (GEN, SIM)}
    path = write_build(
        site.proj,
        U2,
        {GEN: "gen", SIM: "sim"},
        [
            ("submitted", GEN, {"job_id": "4241", "attempt": 1, "batch_size": 1}),
            ("finished", GEN, {"state": "done", "exit_code": 0}),
            ("submitted", SIM, {"job_id": JOB, "attempt": 1, "batch_size": 1}),
            ("finished", SIM, {"state": "failed", "exit_code": 1}),
        ],
        build=int(build),
        status="failed",
    )
    record = BuildRecord.read(path)
    record.model_copy(update={"plan": first, "final_plan": final}).write(path)
    return ReproBuild(
        build=int(build),
        uuid=U2,
        cas=cas,
        plan=plan,
        results={GEN: gen_result, SIM: sim_result},
        kept=kept,
        debug_root=site.root / "debug",
    )
