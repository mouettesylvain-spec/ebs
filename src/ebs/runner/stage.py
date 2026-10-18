"""Scratch directories and input staging (strict staging, sandbox v1; invariant I10).

`work/` holds exactly the declared inputs at their logical paths plus the action's config files;
every input is re-hashed after materialization and must match its expected content digest
before the tool may run. Inputs above `[cas].copy_threshold` are symlinked read-only from the
CAS (a 100 GB database is never copied just to be read) unless declared `writable` (P1-04).

Under SLURM the scratch dir holds a marker, `.ebs-job`, with the job id: the node epilog
(deploy/slurm/epilog.d) removes the scratch of a job the runner could not clean (SIGKILL).
"""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

from ebs.cas.api import CAS
from ebs.core.digest import Digest, hash_bytes, hash_file
from ebs.core.errors import CasError, MetadataError, TreeError
from ebs.core.tree import build_tree
from ebs.meta.api import MetadataStore
from ebs.plan.types import ActionOutputInput, ActionSpec, ImportInput, InputRef, Plan
from ebs.runner.errors import InfraError, InputVerificationError, UsageError

__all__ = [
    "JOB_MARKER",
    "Scratch",
    "action_dir_name",
    "prepare_work",
    "remove_tree",
    "resolve_content",
    "resolve_contents",
    "stage_inputs",
    "verify_inputs",
    "write_config_files",
]

JOB_MARKER: Final = ".ebs-job"
"""File in an action's scratch root holding the SLURM job id (read by the node epilog)."""
_CREATE_ATTEMPTS: Final = 5


def action_dir_name(action_id: str) -> str:
    """A fixed-length, filesystem-safe directory name for an action id."""
    return hash_bytes(action_id.encode("utf-8")).hex[:16]


@dataclass(frozen=True, slots=True)
class Scratch:
    """`<scratch>/<build>/<action-hash>/{work,home,tmp,logs}` on node-local disk."""

    root: Path
    work: Path
    home: Path
    tmp: Path
    logs: Path

    @classmethod
    def create(
        cls, scratch_dir: Path, build: str, action_id: str, *, job_id: str | None = None
    ) -> Scratch:
        """Create the dirs; with `job_id`, first mark the root for the node epilog."""
        root = scratch_dir / build / action_dir_name(action_id)
        if os.path.lexists(root):  # left over from an earlier attempt killed with SIGKILL
            _rmtree(root)
        # Another runner of this build on the node may rmdir the (empty) build dir between our
        # creating it and creating the action dir inside it; once `root` exists it cannot.
        for attempt in range(_CREATE_ATTEMPTS):
            try:
                root.mkdir(parents=True, mode=0o700)
                break
            except FileNotFoundError:
                if attempt == _CREATE_ATTEMPTS - 1:
                    raise
        dirs = cls(root, root / "work", root / "home", root / "tmp", root / "logs")
        try:
            if job_id is not None:  # before anything big lands here
                (root / JOB_MARKER).write_text(f"{job_id}\n", encoding="ascii")
            for d in (dirs.work, dirs.home, dirs.tmp, dirs.logs):
                d.mkdir(mode=0o700)
        except OSError:
            dirs.remove()
            raise
        return dirs

    def remove(self) -> None:
        """Delete the action's scratch, and the build directory once it is empty."""
        _rmtree(self.root)
        with contextlib.suppress(OSError):
            self.root.parent.rmdir()


def remove_tree(path: Path) -> None:
    """Delete `path` (a file, link or tree), also read-only directories a tool left behind."""
    _rmtree(path)


def _rmtree(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_symlink():
        path.unlink()
        return
    # A tool may leave read-only directories behind; unlinking needs write access to each one.
    path.chmod(0o700)
    for dirpath, dirnames, _ in os.walk(path):
        for name in dirnames:
            sub = os.path.join(dirpath, name)
            if not os.path.islink(sub):
                os.chmod(sub, 0o700)
    shutil.rmtree(path)


def resolve_contents(
    plan: Plan, spec: ActionSpec, store: MetadataStore | None
) -> dict[str, Digest]:
    """The content digest to stage for each input (logical path -> blob or tree digest).

    An input id is its content digest, except for an output of a `deterministic: false`
    producer: that id is derived from the producer's key, and the bytes are found through the
    producer's recorded result (`MetadataStore.resolve_output`). An imported release output
    carries its content in the plan (`ImportInput.content`).
    """
    contents: dict[str, Digest] = {}
    for ref in spec.inputs:
        content = resolve_content(plan, spec, ref, store)
        if content is None:
            raise InfraError(
                f"input {ref.logical_path!r} ({ref.id}) has no recorded result in domain "
                f"{spec.domain!r}; its producer's result is missing (not yet posted, or collected)"
            )
        contents[ref.logical_path] = content
    return contents


def resolve_content(
    plan: Plan, spec: ActionSpec, ref: InputRef, store: MetadataStore | None
) -> Digest | None:
    """The content digest of one input; None if a nondeterministic producer's result is gone."""
    if ref.id is None:
        raise UsageError(
            f"input {ref.logical_path!r} of action {spec.action_id!r} has no id yet: the "
            "driver must refine the plan with its producer's result before running it"
        )
    if isinstance(ref.source, ImportInput):
        return ref.source.content  # the release recorded it: no store lookup (P2-03 R3)
    if not isinstance(ref.source, ActionOutputInput) or _deterministic(plan, ref.source):
        return ref.id
    if store is None:
        raise UsageError(
            f"input {ref.logical_path!r} is a nondeterministic output; finding its bytes needs "
            "the metadata store: configure [metadata].url"
        )
    try:
        return store.resolve_output(spec.domain, ref.id)
    except MetadataError as exc:
        raise InfraError(f"cannot resolve input {ref.logical_path!r}: {exc}") from exc


def _deterministic(plan: Plan, source: ActionOutputInput) -> bool:
    try:
        return plan.action(source.action_id).output(source.output).deterministic
    except KeyError:
        raise UsageError(
            f"the plan has no output {source.output!r} of action {source.action_id!r}, "
            "which an input refers to; the plan is inconsistent: re-plan"
        ) from None


def stage_inputs(
    spec: ActionSpec,
    cas: CAS,
    work: Path,
    contents: Mapping[str, Digest],
    *,
    copy_threshold: int,
) -> None:
    """Materialize every input at `work/<logical path>`; missing CAS objects are InfraError.

    Files up to `copy_threshold` bytes are copied, larger ones symlinked read-only from the CAS
    (tree files are hard-linked when the CAS shares the filesystem); `writable` inputs are
    always private writable copies.
    """
    for ref in spec.inputs:
        try:
            cas.materialize(
                contents[ref.logical_path],
                ref.kind,
                work / ref.logical_path,
                mode="auto",
                copy_threshold=copy_threshold,
                writable=ref.writable,
            )
        except CasError as exc:
            raise InfraError(f"cannot stage input {ref.logical_path!r}: {exc}") from exc


def prepare_work(
    spec: ActionSpec,
    cas: CAS,
    work: Path,
    contents: Mapping[str, Digest],
    *,
    copy_threshold: int,
) -> None:
    """Stage the inputs, write the config files and verify every input digest (I10).

    The runner and `ebs reproduce` (P1-10) both build an action's work dir with this, so a
    reproduced sandbox holds exactly what the tool saw on the node.
    """
    stage_inputs(spec, cas, work, contents, copy_threshold=copy_threshold)
    write_config_files(spec, work)
    verify_inputs(spec, work, contents)


def write_config_files(spec: ActionSpec, work: Path) -> None:
    """Write the action's config files (part of its key) read-only at their logical paths."""
    for logical_path, content in spec.config_files.items():
        path = work / logical_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        path.chmod(0o444)


def verify_inputs(spec: ActionSpec, work: Path, contents: Mapping[str, Digest]) -> None:
    """Re-hash what was staged (I10); InputVerificationError lists every mismatch."""
    problems = []
    for ref in spec.inputs:
        expected = contents[ref.logical_path]
        path = work / ref.logical_path
        try:
            if ref.kind == "file":
                actual = hash_file(path, expected.algo)[0]
            else:
                actual = build_tree(path, partial(hash_file, algo=expected.algo))[0]
        except (OSError, TreeError) as exc:
            problems.append(f"{ref.logical_path}: cannot hash the staged {ref.kind}: {exc}")
            continue
        if actual != expected:
            problems.append(f"{ref.logical_path}: expected {expected}, staged {actual}")
    if problems:
        raise InputVerificationError(
            f"input verification failed for action {spec.action_id!r} (the CAS copy is corrupt "
            "or changed); nothing was executed:\n  " + "\n  ".join(problems)
        )
