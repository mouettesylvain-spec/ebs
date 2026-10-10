"""A finished build with outputs, for `ebs outputs` / `ebs fetch` tests (P1-13).

Actions (step in brackets), with the state their events end in:

- `gen` (gen): done; `report` file at `reports/gen.rpt`, `model` tree at `model`
- `sim[test=smoke]` (sim): cached; `result` file at `result.json`
- `sim[test=random]` (sim): failed (the tool reported FAIL); `result` file at `result.json`
- `x[v=a/b%c.d]` (x): done; `out` file at `out.txt` (an id that needs encoding)
- `late` (late): still running, no result
- `gone` (gone): done, but its `out` blob is not in the CAS (garbage-collected)

The final plan (in the CAS, named by `build.json`) declares the paths above; the submitted
`plan` puts them under `submitted/` (with `final_plan=False` the plan declares the paths above
and there is no final plan). The result of `gen` also has an `extra` output no plan declares.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ebs.cas.fs import FsCAS
from ebs.cli._records import BuildRecord, build_dir
from ebs.core.digest import Digest, hash_bytes
from ebs.meta.api import ActionRow, BuildCreate, OutputResult, ResultManifest
from ebs.plan.keys import with_key
from ebs.plan.planfile import encode
from ebs.plan.types import OutputSpec, OutputType, Plan
from tests.helpers.cli import Site, write_build
from tests.helpers.driver import Node, make_plan, result_for

U1 = UUID("11111111-1111-4111-8111-111111111111")
ODD = "x[v=a/b%c.d]"
ACTIONS = {
    "gen": "gen",
    "sim[test=smoke]": "sim",
    "sim[test=random]": "sim",
    ODD: "x",
    "late": "late",
    "gone": "gone",
}
PATHS: dict[tuple[str, str], tuple[str, OutputType]] = {
    ("gen", "report"): ("reports/gen.rpt", "file"),
    ("gen", "model"): ("model", "dir"),
    ("sim[test=smoke]", "result"): ("result.json", "file"),
    ("sim[test=random]", "result"): ("result.json", "file"),
    (ODD, "out"): ("out.txt", "file"),
    ("late", "out"): ("out.txt", "file"),
    ("gone", "out"): ("out.txt", "file"),
}
CONTENT = {
    ("gen", "report"): b"0 violation(s)\n",
    ("gen", "extra"): b"not in the plan\n",
    ("sim[test=smoke]", "result"): b'{"status": "pass"}\n',
    ("sim[test=random]", "result"): b'{"status": "fail"}\n',
    (ODD, "out"): b"odd\n",
}
MODEL = {"top.v": b"module top; endmodule\n", "sub/lib.v": b"module lib; endmodule\n"}


@dataclass(frozen=True)
class Built:
    build: int
    dir: Path  # .ebs/builds/<uuid>
    cas: FsCAS
    results: dict[str, ResultManifest]


def _nodes() -> dict[str, Node]:
    outputs: dict[str, dict[str, bool]] = {}
    for action_id, name in PATHS:
        outputs.setdefault(action_id, {})[name] = True
    return {a: Node(outputs=outputs[a], step=ACTIONS[a]) for a in ACTIONS}


def edit_outputs(plan: Plan, edit: Callable[[str, OutputSpec], OutputSpec]) -> Plan:
    """`plan` with each output replaced by `edit(action_id, output)`, re-keyed (decode checks)."""
    return dataclasses.replace(
        plan,
        actions=tuple(
            with_key(
                dataclasses.replace(
                    s, outputs=tuple(edit(s.action_id, o) for o in s.outputs), key=None
                )
            )
            for s in plan.actions
        ),
    )


def write_outputs_build(site: Site, *, final_plan: bool = True) -> Built:
    """The build described in the module docstring; `final_plan=False` leaves it unset."""
    cas = FsCAS(site.root / "cas", "test", clock=site.services.clock)
    plan = make_plan(cas, _nodes())
    plan = edit_outputs(
        plan,
        lambda a, o: dataclasses.replace(o, path=PATHS[(a, o.name)][0], type=PATHS[(a, o.name)][1]),
    )
    plan_digest = cas.put_bytes(encode(plan))
    submitted = cas.put_bytes(
        encode(edit_outputs(plan, lambda a, o: dataclasses.replace(o, path="submitted/" + o.path)))
    )
    store = site.services.memory_store
    build = store.create_build(
        BuildCreate(
            uuid=U1, domain="test", project="demo", plan_digest=plan_digest,
            flow_repo="", flow_commit="", flow_dirty=False, user_name="alice", cache_mode="read",
        )
    )  # fmt: skip
    specs = {s.action_id: s for s in plan.actions}
    store.add_actions(
        build, [ActionRow(action_id=a, step=s, key=specs[a].key) for a, s in ACTIONS.items()]
    )
    tree_src = site.root / "model-src"
    for rel, data in MODEL.items():
        (tree_src / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree_src / rel).write_bytes(data)
    tree = cas.put_tree(tree_src)
    results: dict[str, ResultManifest] = {}
    for action_id in ("gen", "sim[test=smoke]", "sim[test=random]", ODD, "gone"):
        spec = specs[action_id]
        outputs: dict[str, OutputResult] = {}
        for (aid, name), data in CONTENT.items():
            if aid == action_id:
                d = cas.put_bytes(data)
                outputs[name] = OutputResult(digest=d, id=d, type="file", size=len(data))
        if action_id == "gen":
            size = sum(len(b) for b in MODEL.values())
            outputs["model"] = OutputResult(digest=tree, id=tree, type="tree", size=size)
        if action_id == "gone":
            d = hash_bytes(b"collected\n")  # never stored
            outputs["out"] = OutputResult(digest=d, id=d, type="file", size=10)
        result = result_for(spec, passed=action_id != "sim[test=random]").model_copy(
            update={"outputs": outputs}
        )
        store.record_result(build, action_id, result)
        results[action_id] = result
    path = write_build(
        site.proj,
        U1,
        ACTIONS,
        [
            ("finished", "gen", {"state": "done", "exit_code": 0}),
            ("cache_hit", "sim[test=smoke]", {}),
            ("finished", "sim[test=random]", {"state": "failed", "exit_code": 1}),
            ("finished", ODD, {"state": "done", "exit_code": 0}),
            ("running", "late", {"job_id": "9"}),
            ("finished", "gone", {"state": "done", "exit_code": 0}),
        ],
        build=int(build),
    )
    record = BuildRecord.read(path)
    record.model_copy(
        update={"plan": submitted, "final_plan": plan_digest}
        if final_plan
        else {"plan": plan_digest, "final_plan": None}
    ).write(path)
    assert path == build_dir(site.proj, U1)
    return Built(int(build), path, cas, results)


def digest_of(built: Built, action_id: str, name: str) -> Digest:
    return built.results[action_id].outputs[name].digest
