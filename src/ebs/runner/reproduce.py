"""Rebuild one action's sandbox outside a build: `ebs reproduce` (P1-10).

The sandbox is made by the runner's own code (`resolve_content`, `prepare_work`,
`toolchain_env`, `build_env`, `run_tool`), so it holds the same staged inputs (copies, read-only
CAS links, config files, every digest verified) and gets the same environment as on the node.
Its layout is the runner's scratch layout, `{work,home,tmp,logs}`, in `<[scratch].dir>/repro/
<action dir>` or in a directory the user names.

Nothing is posted (R4): no build, result, cache entry, event, lease or access record; the
metadata store is only read (`resolve_output`). The sandbox's `repro.json` says so (`kind:
"repro"`, `cache_mode: "off"`) and names the build and action it reproduces. The sandbox is kept
for the user to inspect; `ebs reproduce` says where.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, Literal

from ebs.cas.api import CAS
from ebs.core.digest import Algo, Digest, hash_file
from ebs.core.errors import CasError
from ebs.core.tree import build_tree
from ebs.meta.api import MetadataStore, OutputResult
from ebs.plan.types import ActionSpec, InputKind, Plan
from ebs.runner.collect import output_paths
from ebs.runner.config import RunnerSettings
from ebs.runner.env import build_env, toolchain_env
from ebs.runner.errors import InfraError, UsageError
from ebs.runner.run import ToolRun, run_tool
from ebs.runner.stage import (
    JOB_MARKER,
    Scratch,
    action_dir_name,
    prepare_work,
    remove_tree,
    resolve_content,
)

__all__ = [
    "REPRO_BUILD_DIR",
    "REPRO_RECORD",
    "MissingInput",
    "MissingInputsError",
    "OutputCheck",
    "Sandbox",
    "build_sandbox",
    "check_outputs",
    "find_missing",
    "identical",
    "run_sandbox",
]

REPRO_BUILD_DIR: Final = "repro"
"""`<[scratch].dir>/repro-<uid>/<action dir>-<random>`: where reproduced sandboxes go."""
REPRO_RECORD: Final = "repro.json"

CheckStatus = Literal["same", "different", "missing", "not_compared", "not_recorded"]


@dataclass(frozen=True, slots=True)
class MissingInput:
    logical_path: str
    kind: InputKind
    id: Digest | None  # None: its producer never ran in this build
    content: Digest | None  # None: unknown (e.g. a nondeterministic input's bytes not recorded)
    reason: str


class MissingInputsError(CasError):
    """Inputs whose bytes are gone (garbage-collected): the action cannot be reproduced."""

    def __init__(self, action_id: str, missing: Sequence[MissingInput]) -> None:
        self.missing = tuple(missing)
        lines = "; ".join(
            f"{m.logical_path} ({m.kind} {m.content or m.id or 'id unknown'}): {m.reason}"
            for m in missing
        )
        super().__init__(
            f"cannot reproduce {action_id}: {len(missing)} input(s) are no longer available: "
            f"{lines}. They were probably garbage-collected; rebuild them with "
            f"`ebs build --no-cache {action_id.split('[', 1)[0]}` (or its producer step), then "
            "reproduce that build"
        )


@dataclass(frozen=True, slots=True)
class Sandbox:
    scratch: Scratch
    env: dict[str, str]
    contents: dict[str, Digest]  # logical path -> content digest staged


@dataclass(frozen=True, slots=True)
class OutputCheck:
    name: str
    status: CheckStatus
    recorded: Digest | None
    reproduced: Digest | None


def find_missing(
    plan: Plan, spec: ActionSpec, cas: CAS, store: MetadataStore | None
) -> tuple[dict[str, Digest], list[MissingInput]]:
    """Every input's content digest, and the inputs whose bytes cannot be found (R5)."""
    contents: dict[str, Digest] = {}
    missing: list[MissingInput] = []
    for ref in spec.inputs:
        if ref.id is None:
            missing.append(
                MissingInput(
                    ref.logical_path, ref.kind, None, None,
                    "its producer never ran in this build (or the build did not finish)",
                )
            )  # fmt: skip
            continue
        content = resolve_content(plan, spec, ref, store)
        if content is None:
            missing.append(
                MissingInput(
                    ref.logical_path, ref.kind, ref.id, None,
                    "its producer's result is not recorded in the metadata store",
                )
            )  # fmt: skip
        elif gone := _absent(cas, content, ref.kind):
            what = (
                "not in the CAS"
                if gone == [content]
                else f"{len(gone)} object(s) of it not in the CAS"
            )
            missing.append(MissingInput(ref.logical_path, ref.kind, ref.id, content, what))
        else:
            contents[ref.logical_path] = content
    return contents, missing


def _absent(cas: CAS, content: Digest, kind: InputKind) -> list[Digest]:
    """The CAS objects of `content` that are gone: for a tree, its manifests and every blob
    (`has` of a tree only sees its root manifest)."""
    if not cas.has(content):
        return [content]
    if kind == "file":
        return []
    gone: list[Digest] = []
    try:
        manifest = cas.get_tree(content)
    except CasError:
        return [content]
    for entry in manifest.entries:
        if entry.digest is None:
            continue
        if entry.type == "dir":
            gone += _absent(cas, entry.digest, "tree")
        elif not cas.has(entry.digest):
            gone.append(entry.digest)
    return gone


def build_sandbox(
    plan: Plan,
    spec: ActionSpec,
    *,
    cas: CAS,
    store: MetadataStore | None,
    settings: RunnerSettings,
    caller_env: Mapping[str, str],
    dest: Path | None = None,
    record: Mapping[str, object] | None = None,
) -> Sandbox:
    """Stage `spec` like the runner does, in `dest` (absent or empty) or in local scratch.

    The default place is a new directory `<[scratch].dir>/repro-<uid>/<action dir>-<random>`
    (mode 0700), never an earlier sandbox. Under SLURM (`$SLURM_JOB_ID` in `caller_env`, e.g.
    `--salloc`) it gets the `.ebs-job` marker, so the node epilog removes it with the job.
    Raises MissingInputsError before creating anything if an input's bytes are gone, and
    UsageError if `dest` holds files. `record` adds fields to `repro.json`.
    """
    tool_env = toolchain_env(plan, spec)
    contents, missing = find_missing(plan, spec, cas, store)
    if missing:
        raise MissingInputsError(spec.action_id, missing)
    existed = False
    try:
        if dest is None:
            root = _new_root(settings.scratch_dir, spec.action_id)
        else:
            existed = _check_dest(dest)
            root = dest
    except OSError as exc:
        raise InfraError(f"cannot create the sandbox directory: {exc}") from exc
    scratch = Scratch(root, root / "work", root / "home", root / "tmp", root / "logs")
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        job_id = caller_env.get("SLURM_JOB_ID") if dest is None else None
        if job_id:
            (root / JOB_MARKER).write_text(f"{job_id}\n", encoding="ascii")
        for d in (scratch.work, scratch.home, scratch.tmp, scratch.logs):
            d.mkdir(mode=0o700)
        prepare_work(spec, cas, scratch.work, contents, copy_threshold=settings.copy_threshold)
        env = build_env(
            spec,
            toolchain_env=tool_env,
            scratch=scratch,
            caller_env=caller_env,
            passthrough_patterns=settings.passthrough_env,
        )
        doc = {
            "v": 1,
            "kind": "repro",
            "cache_mode": "off",
            **(record or {}),
            "action_id": spec.action_id,
            "key": None if spec.key is None else str(spec.key),
        }
        (scratch.root / REPRO_RECORD).write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _discard(scratch, existed)
        raise InfraError(f"cannot build the sandbox in {root}: {exc}") from exc
    except BaseException:
        _discard(scratch, existed)
        raise
    return Sandbox(scratch, env, contents)


def _new_root(scratch_dir: Path, action_id: str) -> Path:
    """A fresh, private directory for one sandbox: earlier ones are kept, other users' never
    touched (the per-user parent is created 0700 and must be ours)."""
    parent = scratch_dir / f"{REPRO_BUILD_DIR}-{os.getuid()}"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = parent.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise UsageError(
            f"{parent} is not a directory of yours; set [scratch].dir to a directory you own"
        )
    return Path(tempfile.mkdtemp(prefix=f"{action_dir_name(action_id)}-", dir=parent))


def _check_dest(root: Path) -> bool:
    """Whether `--dir` exists (then empty); UsageError if it holds anything."""
    if not (root.exists() or root.is_symlink()):
        return False
    if root.is_symlink() or not root.is_dir():
        raise UsageError(f"--dir {root} exists and is not a directory")
    if any(root.iterdir()):
        raise UsageError(
            f"--dir {root} is not empty; give a new or empty directory (nothing was changed)"
        )
    return True


def _discard(scratch: Scratch, existed: bool) -> None:
    """Undo a sandbox that could not be built: never a directory or file the user had."""
    with contextlib.suppress(OSError):
        if existed:
            for child in list(scratch.root.iterdir()):
                remove_tree(child)
        else:
            remove_tree(scratch.root)


def run_sandbox(spec: ActionSpec, sandbox: Sandbox, settings: RunnerSettings) -> ToolRun:
    """Run the action's command in the sandbox as the runner does (log: `logs/tool.log`)."""
    time = spec.resources.time
    if time is not None and not isinstance(time, int):
        raise UsageError(
            f"action {spec.action_id!r} has an unexpanded time limit {time!r}; the plan must "
            "hold seconds"
        )
    return run_tool(
        spec.argv,
        cwd=sandbox.scratch.work,
        env=sandbox.env,
        log_path=sandbox.scratch.logs / "tool.log",
        timeout_s=time if isinstance(time, int) else None,
        max_log=settings.max_log,
        grace_s=settings.kill_grace_s,
    )


def check_outputs(
    spec: ActionSpec, work: Path, recorded: Mapping[str, OutputResult] | None
) -> list[OutputCheck]:
    """Hash the outputs left in `work` (nothing is stored) and compare them with `recorded`.

    Deterministic outputs must have the recorded content digest (R3); `deterministic: false`
    ones are hashed but not compared.
    """
    recorded = recorded or {}
    present = output_paths(spec, work)
    checks = []
    for out in spec.outputs:
        rec = recorded.get(out.name)
        path = present.get(out.name)
        if path is None:
            if rec is not None or not out.optional:
                checks.append(OutputCheck(out.name, "missing", _digest(rec), None))
            continue
        algo: Algo = rec.digest.algo if rec is not None else "sha256"
        if out.type == "file":
            got = hash_file(path, algo)[0]
        else:
            got = build_tree(path, partial(hash_file, algo=algo))[0]
        status: CheckStatus
        if not out.deterministic:
            status = "not_compared"
        elif rec is None:
            status = "not_recorded"
        else:
            status = "same" if rec.digest == got else "different"
        checks.append(OutputCheck(out.name, status, _digest(rec), got))
    return checks


def _digest(result: OutputResult | None) -> Digest | None:
    return None if result is None else result.digest


def identical(checks: Sequence[OutputCheck]) -> bool:
    """Every deterministic output reproduced byte for byte, none missing or extra."""
    return all(c.status in ("same", "not_compared") for c in checks)
