"""Hash and upload outputs and the tool log to the CAS; copy debug files to the debug area.

Debug area (architecture.md § Runner lifecycle, step 6): `<debug root>/<domain>/<build>/<action>/`
holds `tool.log`, `command.json` (argv, env, inputs), `live.log` (`ebs.runner.livelog`) and the
files matched by `debug.collect` under `work/`. The domain dir is the admin's (group `<domain>`,
mode 2750); everything below it is created in that dir's group with mode 2750 (dirs) or 0640
(files), whatever the umask (R6), so NDA logs never become readable by another group.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, Literal
from urllib.parse import quote

from ebs.cas.api import CAS
from ebs.core.digest import Digest
from ebs.meta.api import BlobRef, OutputResult
from ebs.plan.types import ActionSpec, DebugSpec, OutputSpec
from ebs.runner.result import output_id

__all__ = [
    "DEBUG_DIR_MODE",
    "DEBUG_FILE_MODE",
    "TRUNCATED_NOTE",
    "Collected",
    "DebugCollected",
    "collect_debug",
    "collect_outputs",
    "debug_action_dir",
    "log_tail",
    "make_debug_dir",
    "open_debug_file",
    "output_paths",
    "stored_objects",
    "wants_debug",
]

LOG_TAIL_BYTES: Final = 64 << 10
DEBUG_DIR_MODE: Final = 0o2750
DEBUG_FILE_MODE: Final = 0o640
TRUNCATED_NOTE: Final = (
    "[ebs-runner: {dropped} bytes dropped at the start by the debug size cap; the tail follows]\n"
)
_ACTION_SAFE: Final = "[]=,+@:%-._~"  # action ids are percent-encoded already (no "/")
_CHUNK: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class Collected:
    outputs: dict[str, OutputResult]
    missing: tuple[str, ...]  # required outputs absent (or of the wrong type), by name


def output_paths(spec: ActionSpec, work: Path) -> dict[str, Path]:
    """Outputs that exist with their declared type: name -> path in the work dir."""
    return {out.name: work / out.path for out in spec.outputs if _present(out, work / out.path)}


def _present(out: OutputSpec, path: Path) -> bool:
    if path.is_symlink():
        return False
    return path.is_file() if out.type == "file" else path.is_dir()


def collect_outputs(spec: ActionSpec, work: Path, cas: CAS, key: Digest) -> Collected:
    """Upload every present output; report required ones that are missing."""
    present = output_paths(spec, work)
    results: dict[str, OutputResult] = {}
    kind: Literal["file", "tree"]
    for out in spec.outputs:
        path = present.get(out.name)
        if path is None:
            continue
        if out.type == "file":
            content = cas.put_file(path)
            size = os.stat(path).st_size
            kind = "file"
        else:
            content = cas.put_tree(path)  # blobs first, manifests last (I9)
            size = sum(e.size for e in cas.get_tree(content).entries)
            kind = "tree"
        results[out.name] = OutputResult(
            digest=content, id=output_id(out, content, key), type=kind, size=size
        )
    missing = tuple(o.name for o in spec.outputs if not o.optional and o.name not in present)
    return Collected(results, missing)


def log_tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    """The last `limit` bytes of the log, decoded leniently, for `RulePlugin.classify`."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - limit))
            return f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def stored_objects(
    cas: CAS, outputs: Mapping[str, OutputResult], log: Digest | None, log_path: Path
) -> list[BlobRef]:
    """Every CAS object the runner stored for this action: output files, output trees with all
    their manifests and blobs, and the log (GC registration, P1-09 R1). A tree's size is the
    total of the file bytes below it, like `OutputResult.size`."""
    found: dict[Digest, BlobRef] = {}

    def tree(digest: Digest, size: int) -> None:
        found[digest] = BlobRef(digest=digest, size=size, kind="tree")
        for entry in cas.get_tree(digest).entries:
            if entry.digest is None or entry.digest in found:
                continue
            if entry.type == "dir":
                tree(entry.digest, entry.size)
            else:
                found[entry.digest] = BlobRef(digest=entry.digest, size=entry.size, kind="file")

    for out in outputs.values():
        if out.type == "tree":
            tree(out.digest, out.size)
        else:
            found[out.digest] = BlobRef(digest=out.digest, size=out.size, kind="file")
    if log is not None:
        found[log] = BlobRef(digest=log, size=os.stat(log_path).st_size, kind="file")
    return [found[d] for d in sorted(found, key=str)]


# --- debug area ---------------------------------------------------------------------------------


def debug_action_dir(root: Path, domain: str, build: str, action_id: str) -> Path:
    """`<root>/<domain>/<build>/<action>`: where an action's debug files go."""
    name = quote(action_id, safe=_ACTION_SAFE)
    if name in (".", ".."):
        name = quote(name, safe="")
    return root / domain / build / name


def make_debug_dir(root: Path, domain: str, build: str, action_id: str) -> Path:
    """Create the action's debug dir (and its build dir) in the domain dir's group, 2750.

    OSError if the domain dir is missing (an admin creates it, D7) or the group cannot be set:
    nothing must then be written, rather than written readable by another group.
    """
    gid = os.stat(root / domain).st_gid
    dest = debug_action_dir(root, domain, build, action_id)
    for d in (dest.parent, dest):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            if d.is_symlink() or not d.is_dir():
                raise NotADirectoryError(f"{d} exists and is not a directory") from None
            if d.stat().st_uid != os.getuid():
                continue  # someone else's (build dir of a shared build): leave it be
        _own_dir(d, gid)
    return dest


def _own_dir(path: Path, gid: int) -> None:
    os.chown(path, -1, gid)
    os.chmod(path, DEBUG_DIR_MODE)  # after chown: a group change may clear the setgid bit


def open_debug_file(path: Path, *, append: bool = False) -> int:
    """Open (create or truncate) a debug file 0640 in its directory's group; returns the fd."""
    gid = os.stat(path.parent).st_gid
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    flags |= os.O_APPEND if append else os.O_TRUNC
    fd = os.open(path, flags, DEBUG_FILE_MODE)
    try:
        os.fchown(fd, -1, gid)
        os.fchmod(fd, DEBUG_FILE_MODE)
    except OSError:
        os.close(fd)
        raise
    return fd


def wants_debug(debug: DebugSpec, status: Literal["passed", "failed", "infra"]) -> bool:
    """Debug files are kept always on failure, on success only with `on_success` (R2)."""
    return status != "passed" or debug.on_success


@dataclass(frozen=True, slots=True)
class DebugCollected:
    files: tuple[str, ...]  # paths relative to the debug dir
    truncated: tuple[str, ...]  # those cut to their tail by the size cap
    unreadable: tuple[str, ...] = ()  # matched but not readable: skipped


def collect_debug(
    dest: Path,
    *,
    work: Path,
    patterns: Sequence[str],
    tool_log: Path,
    command: Mapping[str, object],
    max_size: int | None,
    exclude: Collection[str] = (),
    reserved: int = 0,
) -> DebugCollected:
    """Copy `command.json`, the tool log and the files `patterns` match in `work` to `dest`.

    Patterns are relative to `work`; a matched directory contributes every file below it.
    Symlinks and paths in (or below) `exclude` (the staged inputs) are skipped, and so are
    files that cannot be read (listed in `unreadable`). `reserved` bytes of the cap are
    already used in `dest` (the live log). With
    `max_size`, the total stays within it: `command.json` is always complete, and the largest
    files are cut to their tail, after a `TRUNCATED_NOTE` line, until the rest fits.
    """
    gid = os.stat(dest).st_gid
    command_bytes = (json.dumps(command, indent=2, sort_keys=True) + "\n").encode()
    _write_bytes(dest / "command.json", command_bytes)
    sources: dict[str, Path] = {}
    if tool_log.is_file():
        sources["tool.log"] = tool_log
    for rel in _matches(work, patterns, exclude):
        sources[f"work/{rel}"] = work / rel
    sizes = {name: os.lstat(path).st_size for name, path in sources.items()}
    budget = None if max_size is None else max(0, max_size - len(command_bytes) - reserved)
    limit = _water_level(list(sizes.values()), budget)

    stale = dest / "work"  # an earlier attempt's files
    if stale.is_dir() and not stale.is_symlink():
        shutil.rmtree(stale)
    written = ["command.json"]
    truncated: list[str] = []
    unreadable: list[str] = []
    for name in sorted(sources):
        size = sizes[name]
        keep, note = size, b""
        if limit is not None and size > limit:
            note = TRUNCATED_NOTE.format(dropped=size).encode()
            keep = max(0, limit - len(note))
            note = TRUNCATED_NOTE.format(dropped=size - keep).encode()
            if keep == 0:
                continue  # not even the note fits
            truncated.append(name)
        target = dest / name
        for parent in reversed(target.relative_to(dest).parents[:-1]):
            sub = dest / parent
            if not sub.is_dir():
                sub.mkdir(mode=0o700)
                _own_dir(sub, gid)
        try:
            src = open(sources[name], "rb")  # noqa: SIM115 - closed by _copy_tail
        except OSError:  # the tool left it unreadable, or removed it: skip just this one
            unreadable.append(name)
            if name in truncated:
                truncated.remove(name)
            continue
        _copy_tail(src, target, keep, note)
        written.append(name)
    return DebugCollected(tuple(written), tuple(truncated), tuple(unreadable))


def _matches(work: Path, patterns: Sequence[str], exclude: Collection[str]) -> list[str]:
    found: set[str] = set()
    for pattern in patterns:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            continue  # the planner rejects these; a hand-made plan gets nothing
        for match in work.glob(pattern):
            if not _inside(work, match):
                continue
            if match.is_file():
                found.add(match.relative_to(work).as_posix())
            elif match.is_dir():
                for dirpath, dirnames, filenames in os.walk(match):  # never follows links
                    dirnames[:] = [d for d in dirnames if not os.path.islink(Path(dirpath, d))]
                    for name in filenames:
                        path = Path(dirpath, name)
                        if stat.S_ISREG(os.lstat(path).st_mode):
                            found.add(path.relative_to(work).as_posix())
    return sorted(p for p in found if not _excluded(p, exclude))


def _inside(work: Path, path: Path) -> bool:
    """`path` is below `work` with no symlink on the way (itself included)."""
    rel = path.relative_to(work)  # lexical: a `..` in a hand-made plan would escape
    if ".." in rel.parts:
        return False
    return not any((work / Path(*rel.parts[: i + 1])).is_symlink() for i in range(len(rel.parts)))


def _excluded(path: str, exclude: Collection[str]) -> bool:
    return any(path == e or path.startswith(e + "/") for e in exclude)


def _water_level(sizes: list[int], budget: int | None) -> int | None:
    """The largest per-file size L with sum(min(s, L)) <= budget; None if all fit."""
    if budget is None or sum(sizes) <= budget:
        return None
    remaining = budget
    ordered = sorted(sizes)
    for i, size in enumerate(ordered):
        left = len(ordered) - i
        if size * left > remaining:
            return remaining // left
        remaining -= size
    return None  # pragma: no cover - the sum check above returned already


def _write_bytes(path: Path, data: bytes) -> None:
    fd = open_debug_file(path)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _copy_tail(f: BinaryIO, dest: Path, keep: int, note: bytes) -> None:
    with f, os.fdopen(open_debug_file(dest), "wb") as out:
        f.seek(max(0, os.fstat(f.fileno()).st_size - keep))
        out.write(note)
        _copy_n(f, out, keep)


def _copy_n(src: BinaryIO, dst: BinaryIO, n: int) -> None:
    while n > 0 and (chunk := src.read(min(_CHUNK, n))):
        dst.write(chunk)
        n -= len(chunk)
