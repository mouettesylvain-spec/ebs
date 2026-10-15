"""`ebs outputs` and `ebs fetch`: list a build's outputs and copy them out of the CAS (P1-13).

Both read only: the build record (`.ebs/builds/<uuid>/build.json`) and its events for states,
`get_result` of the metadata store for what each action produced, and the build's final plan
(`final_plan`, else `plan`, from the CAS) for each output's declared logical path. Nothing is
written to the store, the cache, the build record or the CAS.

`fetch` places each output at `DIR/<action dir>/<logical path>` (`--flat`: `DIR/<logical
path>`) with `CAS.materialize`. Every destination is computed and checked first (inside `DIR`,
no two the same, not existing unless `--force`), so these errors leave the disk untouched.
"""

from __future__ import annotations

import enum
import os
import posixpath
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import typer
from rich.table import Table
from rich.text import Text

from ebs.cas.fs import FsCAS
from ebs.cli._context import SiteServices
from ebs.cli._output import Output, handle_errors, make_output
from ebs.cli._records import BuildRecord, action_states, find_build
from ebs.cli.logs import match_action
from ebs.cli.plan import JSON_OPTION
from ebs.core.errors import CasError, ConfigError, MetadataError, PlanError
from ebs.meta.api import MetadataStore, OutputResult, ResultManifest
from ebs.plan.planfile import decode

__all__ = ["action_dir", "fetch_command", "outputs_command"]

DEFAULT_DIR: Final = "ebs-out"
_RESULT_STATES: Final = frozenset({"done", "cached", "failed"})


class FetchMode(enum.StrEnum):
    copy = "copy"
    hardlink = "hardlink"
    symlink = "symlink"


ACTIONS_ARGUMENT = typer.Argument(
    None, metavar="[ACTION]...", help="Action ids or unique prefixes; default: all."
)
TARGETS_ARGUMENT = typer.Argument(
    ...,
    metavar="ACTION[.OUTPUT]...",
    help="Action id, unique prefix or step name; .OUTPUT picks one output (default: all).",
)
DIR_OPTION = typer.Option(
    Path(DEFAULT_DIR), "-o", "--output-dir", metavar="DIR", help="Where to put the outputs."
)
MODE_OPTION = typer.Option(
    FetchMode.copy,
    "--mode",
    help="copy (independent files), or hardlink/symlink into the CAS.",
)


def action_dir(action_id: str) -> str:
    """The directory name of an action under `DIR`: the id with `%` and `/` percent-encoded.

    Readable (`lint[block=alu]` stays as is) and injective; `.` and `..` are encoded as well,
    so no id names `DIR` itself or its parent.
    """
    name = action_id.replace("%", "%25").replace("/", "%2F")
    return name.replace(".", "%2E") if name in (".", "..") else name


@dataclass(frozen=True, slots=True)
class _Row:
    action_id: str
    step: str
    state: str
    name: str
    path: str | None  # declared logical path; None if the plan does not declare it
    output: OutputResult


class _Build:
    """One build's record, results and declared output paths."""

    def __init__(self, services: SiteServices, out: Output, ref: str | None) -> None:
        self.services = services
        self.record: BuildRecord = find_build(services.cwd, ref)
        store = services.store(required=True)
        assert store is not None
        self.store: MetadataStore = store
        self.states = action_states(self.record.actions, self.record.events(services.cwd))
        self.paths = self._declared_paths(out)

    def cas(self) -> FsCAS:
        return self.services.cas(self.record.domain)

    def _declared_paths(self, out: Output) -> dict[tuple[str, str], str]:
        digest = self.record.final_plan or self.record.plan
        try:
            with self.cas().open(digest) as f:
                plan = decode(f.read())
        except (CasError, PlanError) as exc:
            out.warn(
                f"cannot read the plan of build {self.record.uuid} ({exc}); output paths are "
                "unknown"
            )
            return {}
        return {(s.action_id, o.name): o.path for s in plan.actions for o in s.outputs}

    def result(self, action_id: str) -> ResultManifest | None:
        return self.store.get_result(self.record.build, action_id)

    def rows(self, action_id: str, result: ResultManifest) -> list[_Row]:
        state = self.states.get(action_id, "queued")
        if state not in _RESULT_STATES:
            state = "failed" if result.status == "failed" else "done"
        step = self.record.actions.get(action_id, "?")
        return [
            _Row(action_id, step, state, name, self.paths.get((action_id, name)), output)
            for name, output in sorted(result.outputs.items())
        ]


# --- ebs outputs ---------------------------------------------------------------------------------


def _row_json(row: _Row) -> dict[str, Any]:
    return {
        "action_id": row.action_id,
        "step": row.step,
        "state": row.state,
        "name": row.name,
        "path": row.path,
        "type": row.output.type,
        "size": row.output.size,
        "digest": str(row.output.digest),
        "id": str(row.output.id),
    }


def _print_rows(out: Output, record: BuildRecord, rows: list[_Row]) -> None:
    out.line(f"build {record.uuid} (id {record.build}): {len(rows)} outputs")
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("ACTION", "OUTPUT", "STATE", "PATH", "TYPE", "SIZE", "DIGEST"):
        # never wrapped: one line per output, greppable even on a narrow terminal
        table.add_column(column, no_wrap=True, overflow="ignore")
    table.columns[5].justify = "right"
    for r in rows:
        cells = (r.action_id, r.name, r.state, r.path or "-", r.output.type, str(r.output.size))
        table.add_row(*(Text(c) for c in (*cells, str(r.output.digest))))  # ids hold "[...]"
    out.out.print(table)


@handle_errors
def outputs_command(
    ctx: typer.Context,
    actions: list[str] = ACTIONS_ARGUMENT,
    build: str | None = typer.Option(
        None, "--build", metavar="BUILD", help="Build UUID (or prefix) or id; default: the latest."
    ),
    step: str | None = typer.Option(None, "--step", metavar="STEP", help="Only this step."),
    json_mode: bool = JSON_OPTION,
) -> None:
    """List the outputs of a build's finished actions (path, type, size, digest)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    b = _Build(services, out, build)
    record = b.record
    if step is not None and step not in record.actions.values():
        steps = ", ".join(sorted(set(record.actions.values())))
        raise ConfigError(f"no step {step!r} in build {record.uuid}; its steps: {steps}")
    wanted = {match_action(a, record.actions) for a in actions or ()}
    rows: list[_Row] = []
    for action_id, action_step in record.actions.items():
        if (wanted and action_id not in wanted) or (step is not None and action_step != step):
            continue
        result = b.result(action_id)
        if result is not None:
            rows.extend(b.rows(action_id, result))
    if json_mode:
        out.emit(
            {
                "v": 1,
                "uuid": str(record.uuid),
                "build": int(record.build),
                "domain": record.domain,
                "outputs": [_row_json(r) for r in rows],
            }
        )
    else:
        _print_rows(out, record, rows)


# --- ebs fetch -----------------------------------------------------------------------------------


def _resolve_actions(given: str, record: BuildRecord) -> list[str]:
    """An exact action id, else every action of the step `given`, else a unique prefix."""
    if given in record.actions:
        return [given]
    of_step = [a for a, s in record.actions.items() if s == given]
    return of_step or [match_action(given, record.actions)]


def _targets(given: str, record: BuildRecord) -> list[tuple[str, str | None]]:
    """(action id, output name or None for all) for one `ACTION[.OUTPUT]` argument."""
    try:
        return [(a, None) for a in _resolve_actions(given, record)]
    except ConfigError as whole:
        head, dot, name = given.rpartition(".")
        if not dot or not head or not name:
            raise
        try:
            actions = _resolve_actions(head, record)
        except ConfigError:
            if "matches several" in str(whole):  # an ambiguous prefix: list the candidates
                raise whole from None
            raise ConfigError(
                f"no action, step or action output {given!r} in build {record.uuid}; "
                "`ebs outputs` lists them"
            ) from None
        return [(a, name) for a in actions]


@dataclass(frozen=True, slots=True)
class _Fetch:
    row: _Row
    shown: Path  # as printed: DIR as given + relative path
    dest: Path  # absolute


def _no_result(build: _Build, action_id: str) -> ConfigError:
    state = build.states.get(action_id, "queued")
    return ConfigError(
        f"{action_id} has no result in build {build.record.uuid} (state {state}); only done, "
        "cached and failed actions have outputs. `ebs status` shows the build"
    )


def _relative(row: _Row, flat: bool) -> Path:
    """`<action dir>/<logical path>` (or just the path), rejected if it leaves `DIR`."""
    declared = row.path if row.path is not None else row.name
    normal = posixpath.normpath(declared)
    if posixpath.isabs(declared) or normal == ".." or normal.startswith("../") or normal == ".":
        raise ConfigError(
            f"output {row.action_id}.{row.name} declares the path {declared!r}, which is "
            "outside the fetch directory; it cannot be fetched by path"
        )
    return Path(normal) if flat else Path(action_dir(row.action_id)) / normal


def _check_inside(base: Path, item: _Fetch) -> None:
    """Refuse a destination reached through a symlink that leaves `base`."""
    real_base = os.path.realpath(base)
    real_parent = os.path.realpath(item.dest.parent)
    if os.path.commonpath([real_base, real_parent]) != real_base:
        raise ConfigError(
            f"{item.shown} would be written outside {base} (through a symlink to "
            f"{real_parent}); remove the link or choose another directory with -o"
        )


def _plan_fetches(
    build: _Build, args: Sequence[str], base: Path, shown_base: Path, flat: bool, force: bool
) -> list[_Fetch]:
    record = build.record
    fetches: list[_Fetch] = []
    seen: set[tuple[str, str]] = set()
    for given in args:
        for action_id, name in _targets(given, record):
            result = build.result(action_id)
            if result is None:
                raise _no_result(build, action_id)
            rows = build.rows(action_id, result)
            if name is not None:
                rows = [r for r in rows if r.name == name]
                if not rows:
                    names = ", ".join(sorted(result.outputs)) or "none"
                    raise ConfigError(f"{action_id} has no output {name!r}; its outputs: {names}")
            for row in rows:
                if (row.action_id, row.name) in seen:
                    continue
                seen.add((row.action_id, row.name))
                rel = _relative(row, flat)
                fetches.append(_Fetch(row, shown_base / rel, base / rel))
    by_dest: dict[Path, _Fetch] = {}
    for item in fetches:
        # the same dest, or one inside the other (a file output `x` and a tree output `x/y`)
        other = next(
            (by_dest[p] for p in (item.dest, *item.dest.parents) if p in by_dest), None
        ) or next((f for d, f in by_dest.items() if item.dest in d.parents), None)
        if other is not None:
            hint = "; drop --flat to give each action its own directory" if flat else ""
            raise ConfigError(
                f"{other.row.action_id}.{other.row.name} ({other.shown}) and "
                f"{item.row.action_id}.{item.row.name} ({item.shown}) would overwrite each "
                f"other{hint}"
            )
        by_dest[item.dest] = item
    for item in fetches:
        _check_inside(base, item)
        for parent in item.dest.parents:  # up to and including DIR (dest is inside it)
            if os.path.lexists(parent) and not parent.is_dir():
                raise ConfigError(
                    f"{parent} is not a directory, so {item.shown} cannot be written below "
                    "it; remove it or use -o to fetch into another directory"
                )
            if parent == base:
                break
        if os.path.lexists(item.dest) and not force:
            raise ConfigError(
                f"{item.shown} already exists; use --force to replace it "
                "(or -o to fetch into another directory)"
            )
    return fetches


def _gc_hint(row: _Row) -> str:
    return f"it may have been garbage-collected; rebuild it with `ebs build --no-cache {row.step}`"


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


@handle_errors
def fetch_command(
    ctx: typer.Context,
    targets: list[str] = TARGETS_ARGUMENT,
    build: str | None = typer.Option(
        None, "--build", metavar="BUILD", help="Build UUID (or prefix) or id; default: the latest."
    ),
    out_dir: Path = DIR_OPTION,
    flat: bool = typer.Option(
        False, "--flat", help="Put outputs at DIR/<path>, without a directory per action."
    ),
    mode: FetchMode = MODE_OPTION,
    force: bool = typer.Option(False, "--force", help="Replace destinations that already exist."),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Copy outputs of a build's actions out of the CAS into DIR/<action>/<path>."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    b = _Build(services, out, build)
    base = services.cwd / out_dir
    fetches = _plan_fetches(b, targets, base, out_dir, flat, force)
    cas = b.cas()
    for item in fetches:  # every object is present before anything is written
        if not cas.has(item.row.output.digest):
            raise CasError(
                f"{item.row.action_id}.{item.row.name} ({item.row.output.digest}) is not in "
                f"the CAS of domain {b.record.domain}: {_gc_hint(item.row)}"
            )
    fetched: list[dict[str, str]] = []
    for item in fetches:
        if os.path.lexists(item.dest):  # --force
            try:
                _remove(item.dest)
            except OSError as exc:
                raise ConfigError(
                    f"--force cannot remove {item.shown}: {exc}; fix its permissions or use -o "
                    "to fetch into another directory"
                ) from None
        try:
            cas.materialize(item.row.output.digest, item.row.output.type, item.dest, mode.value)
        except CasError as exc:
            raise CasError(
                f"cannot fetch {item.row.action_id}.{item.row.name} to {item.shown}: {exc}; "
                f"if an object is missing, {_gc_hint(item.row)}"
            ) from None
        if not json_mode:
            out.line(f"{item.row.action_id}.{item.row.name} -> {item.shown}")
        fetched.append(
            {
                "action_id": item.row.action_id,
                "name": item.row.name,
                "digest": str(item.row.output.digest),
                "dest": str(item.shown),
            }
        )
    try:  # a fetch is a use: GC keeps recently used objects (P1-09 R1)
        b.store.touch(b.record.domain, [item.row.output.digest for item in fetches])
    except MetadataError as exc:
        out.warn(f"could not record the use of the fetched outputs for GC: {exc}")
    if json_mode:
        out.emit(
            {"v": 1, "uuid": str(b.record.uuid), "build": int(b.record.build), "fetched": fetched}
        )
