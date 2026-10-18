"""`flow.lock`: the exact releases a flow imports (P2-03 R1, R2, R4, R5).

A flow names its imports by channel or version (`imports: {rtl: {from: rtl-team/cpu,
channel: stable}}`); `flow.lock`, next to `flow.yaml` and committed with it, pins each alias to
one release. Only `ebs lock update` resolves channels and versions (`resolve_lock`) and writes
the file; planning reads the pins (`plan_lock`) and fails on a missing or stale entry, so moving
a channel never changes a consumer's build until the consumer updates its lock.

Format (YAML, keys sorted at every level, written by `dump_lock` only):

    # flow.lock: …
    imports:
      rtl:
        channel: stable                 # what it was resolved from (absent: by version)
        domain: cpu-nda                 # the consumer's domain (an exported copy)
        name: rtl-team/cpu
        outputs:
          pack.srcs: {digest: sha256:…, id: sha256:…, size: 1234, type: tree}
        release: sha256:…               # release_digest
        version: '2026.09.3'
    version: 1

`lock_digest` hashes the content (canonical JSON), not the bytes, and is recorded as plan.json
`lock`. `release_digest` covers a release's name, version and outputs, not its domain, so an
exported copy pins the same digest as the original.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ebs.core.canon import digest_json
from ebs.core.digest import Digest
from ebs.core.errors import ReleaseError
from ebs.flow.model import Flow
from ebs.meta.api import (
    CHANNEL_PATTERN,
    DOMAIN_PATTERN,
    IMPORT_ALIAS_PATTERN,
    RELEASE_NAME_PATTERN,
    VERSION_PATTERN,
    DigestField,
    ReleaseImport,
    ReleaseOutput,
    ReleaseStore,
    ReleaseView,
)
from ebs.plan.types import ImportedOutput, ImportPin, PlanLock

__all__ = [
    "LOCK_FILE",
    "LockChange",
    "LockEntry",
    "LockFile",
    "LockedOutput",
    "diff_locks",
    "dump_lock",
    "import_rows",
    "load_lock",
    "lock_digest",
    "lock_entry",
    "parse_lock",
    "plan_lock",
    "release_digest",
    "resolve_lock",
]

LOCK_FILE: Final = "flow.lock"
HEADER: Final = (
    "# flow.lock: the exact releases this flow imports. Written by `ebs lock update`; review\n"
    "# changes like code. `ebs lock check` fails when it is out of date.\n"
)
_FIX: Final = "run `ebs lock update` to rewrite it"

_LOCK = ConfigDict(frozen=True, extra="forbid", strict=True)


class LockedOutput(BaseModel):
    """One output of a pinned release: its content, the id passed downstream, type and size."""

    model_config = _LOCK

    digest: DigestField
    id: DigestField
    size: Annotated[StrictInt, Field(ge=0)]
    type: Literal["file", "tree"]


class LockEntry(BaseModel):
    """The release one import alias is pinned to."""

    model_config = _LOCK

    domain: Annotated[StrictStr, Field(pattern=DOMAIN_PATTERN)]
    name: Annotated[StrictStr, Field(pattern=RELEASE_NAME_PATTERN)]
    version: Annotated[StrictStr, Field(pattern=VERSION_PATTERN)]
    channel: Annotated[StrictStr, Field(pattern=CHANNEL_PATTERN)] | None = None
    release: DigestField
    outputs: Annotated[dict[StrictStr, LockedOutput], Field(min_length=1)]

    @property
    def label(self) -> str:
        via = f" (channel {self.channel})" if self.channel else ""
        return f"{self.name} {self.version}{via}"


_Alias = Annotated[StrictStr, Field(pattern=IMPORT_ALIAS_PATTERN)]


class LockFile(BaseModel):
    model_config = _LOCK

    version: Literal[1] = 1
    imports: dict[_Alias, LockEntry] = {}


def _json(lock: LockFile) -> dict[str, object]:
    """The lock as sorted plain data (what is written and hashed)."""

    def sort(value: object) -> object:
        if isinstance(value, dict):
            return {k: sort(value[k]) for k in sorted(value)}
        return value

    data = lock.model_dump(mode="json", exclude_none=True)
    result = sort(data)
    assert isinstance(result, dict)
    return result


def lock_digest(lock: LockFile) -> Digest:
    return digest_json(_json(lock))  # type: ignore[arg-type]  # plain JSON data


def _release_digest(
    name: str, version: str, outputs: Mapping[str, ReleaseOutput | LockedOutput]
) -> Digest:
    return digest_json(
        {
            "v": 1,
            "name": name,
            "version": version,
            "outputs": {
                n: {"digest": str(o.digest), "id": str(o.id), "size": o.size, "type": o.type}
                for n, o in outputs.items()
            },
        }
    )


def release_digest(release: ReleaseView) -> Digest:
    """A release's identity for a lock: name, version and outputs (not domain, id or authors)."""
    return _release_digest(release.name, release.version, release.outputs)


def _yaml() -> YAML:
    yaml = YAML(typ="safe", pure=True)
    yaml.default_flow_style = False
    yaml.width = 4096  # never fold a line
    return yaml


def dump_lock(lock: LockFile) -> str:
    buf = io.StringIO()
    _yaml().dump(_json(lock), buf)
    return HEADER + buf.getvalue()


def parse_lock(text: str, *, file: str = LOCK_FILE) -> LockFile:
    """Parse and validate a lock file; ReleaseError (naming the file and the fix) if invalid."""
    try:
        data = _yaml().load(text)
    except YAMLError as exc:
        raise ReleaseError(f"{file}: not valid YAML ({exc}); {_FIX}") from None
    try:
        lock = LockFile.model_validate(data if data is not None else {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(top)'}: {e['msg']}" for e in exc.errors()
        )
        raise ReleaseError(f"{file}: invalid lock file: {problems}; {_FIX}") from None
    for alias, entry in lock.imports.items():
        # The planner stages `outputs`; builds record `release`: they must be the same release.
        if _release_digest(entry.name, entry.version, entry.outputs) != entry.release:
            raise ReleaseError(
                f"{file}: imports.{alias}: the outputs do not belong to the pinned release "
                f"{entry.name} {entry.version} (the file was edited by hand); run "
                f"`ebs lock update {alias}`"
            )
    return lock


def load_lock(path: Path) -> LockFile | None:
    """The lock at `path`, or None if there is none."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc
    return parse_lock(text, file=str(path))


def plan_lock(lock: LockFile) -> PlanLock:
    """The pins as the planner takes them."""
    return PlanLock(
        digest=lock_digest(lock),
        imports={
            alias: ImportPin(
                domain=e.domain,
                name=e.name,
                version=e.version,
                channel=e.channel,
                release=e.release,
                outputs={n: ImportedOutput(o.id, o.digest, o.type) for n, o in e.outputs.items()},
            )
            for alias, e in lock.imports.items()
        },
    )


def lock_entry(release: ReleaseView, *, channel: str | None) -> LockEntry:
    return LockEntry(
        domain=release.domain,
        name=release.name,
        version=release.version,
        channel=channel,
        release=release_digest(release),
        outputs={
            n: LockedOutput(digest=o.digest, id=o.id, size=o.size, type=o.type)
            for n, o in release.outputs.items()
        },
    )


def _export_hint(name: str, domain: str) -> str:
    return (
        f"release it in domain {domain!r}, or copy a release of another domain here with "
        f"`ebs release export {name} VERSION --to {domain}`"
    )


def _resolve(store: ReleaseStore, flow: Flow, alias: str) -> LockEntry:
    imp = flow.imports[alias]
    domain, name = flow.domain, imp.from_
    where = f"imports.{alias}"
    if imp.channel is not None:
        found = store.get_channel(domain, name, imp.channel)
        if found is None:
            raise ReleaseError(
                f"{where}: no channel {name}:{imp.channel} in domain {domain!r}; point it at a "
                f"release with `ebs channel set {name} {imp.channel} VERSION --domain {domain}`, "
                "or import a version"
            )
        release = found.release
    else:
        assert imp.version is not None  # the flow model requires channel or version
        got = store.get_release(domain, name, imp.version)
        if got is None:
            raise ReleaseError(
                f"{where}: no release {name} {imp.version} in domain {domain!r}; "
                + _export_hint(name, domain)
            )
        release = got
    if release.yanked_at is not None:
        raise ReleaseError(
            f"{where}: release {release.name} {release.version} is yanked: "
            f"{release.yank_reason}; import another version or move the channel"
        )
    return lock_entry(release, channel=imp.channel)


def resolve_lock(
    store: ReleaseStore, flow: Flow, old: LockFile | None, aliases: Sequence[str] = ()
) -> LockFile:
    """The lock `ebs lock update [ALIAS…]` writes: every import of `flow` resolved now, or with
    `aliases` only those (the others keep their old pin when they have one). Entries of aliases
    the flow no longer imports are dropped."""
    unknown = [a for a in aliases if a not in flow.imports]
    if unknown:
        known = ", ".join(sorted(flow.imports)) or "none"
        raise ReleaseError(
            f"the flow has no import {', '.join(map(repr, unknown))}; its imports: {known}"
        )
    previous = old.imports if old is not None else {}
    entries: dict[str, LockEntry] = {}
    for alias in sorted(flow.imports):
        if aliases and alias not in aliases and alias in previous:
            entries[alias] = previous[alias]
        else:
            entries[alias] = _resolve(store, flow, alias)
    return LockFile(imports=entries)


@dataclass(frozen=True, slots=True)
class LockChange:
    """How one alias changes between two locks (`lock update` / `lock check` output)."""

    alias: str
    change: Literal["added", "removed", "changed"]
    old: LockEntry | None
    new: LockEntry | None
    outputs_changed: tuple[str, ...] = ()
    outputs_added: tuple[str, ...] = ()
    outputs_removed: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.new is None:
            assert self.old is not None
            return f"{self.alias}: removed (was {self.old.name} {self.old.version})"
        if self.old is None:
            return f"{self.alias}: added {self.new.label}"
        via = f" (channel {self.new.channel})" if self.new.channel else ""
        if self.old.name == self.new.name:
            head = f"{self.new.name} {self.old.version} → {self.new.version}"
        else:
            head = f"{self.old.name} {self.old.version} → {self.new.name} {self.new.version}"
        parts = [f"{self.alias}: {head}{via}"]
        for verb, names in (("changed", self.outputs_changed), ("added", self.outputs_added),
                            ("removed", self.outputs_removed)):  # fmt: skip
            if names:
                parts.append(f"{verb} {', '.join(names)}")
        if self.old.domain != self.new.domain:
            parts.append(f"domain {self.old.domain} → {self.new.domain}")
        return "; ".join(parts)

    def to_json(self) -> dict[str, object]:
        entry = self.new or self.old
        assert entry is not None
        return {
            "alias": self.alias,
            "change": self.change,
            "name": entry.name,
            "old_version": None if self.old is None else self.old.version,
            "new_version": None if self.new is None else self.new.version,
            "outputs_changed": list(self.outputs_changed),
            "outputs_added": list(self.outputs_added),
            "outputs_removed": list(self.outputs_removed),
        }


def diff_locks(old: LockFile | None, new: LockFile) -> list[LockChange]:
    """Per alias (sorted): what `new` changes; unchanged aliases are left out."""
    before = old.imports if old is not None else {}
    changes = []
    for alias in sorted(before.keys() | new.imports.keys()):
        a, b = before.get(alias), new.imports.get(alias)
        if a == b:
            continue
        if a is None or b is None:
            changes.append(LockChange(alias, "added" if a is None else "removed", a, b))
            continue
        changes.append(
            LockChange(
                alias, "changed", a, b,
                outputs_changed=tuple(sorted(
                    n for n in a.outputs.keys() & b.outputs.keys() if a.outputs[n] != b.outputs[n]
                )),
                outputs_added=tuple(sorted(b.outputs.keys() - a.outputs.keys())),
                outputs_removed=tuple(sorted(a.outputs.keys() - b.outputs.keys())),
            )
        )  # fmt: skip
    return changes


def import_rows(store: ReleaseStore, flow: Flow, lock: LockFile) -> list[ReleaseImport]:
    """The `release_imports` rows of a build of `flow` planned with `lock` (R5): each pinned
    release, found in the store and checked against its pinned digest."""
    rows = []
    for alias in sorted(flow.imports):
        entry = lock.imports[alias]  # the planner checked every import has its pin
        found = store.get_release(entry.domain, entry.name, entry.version)
        if found is None or release_digest(found) != entry.release:
            what = "does not exist" if found is None else "has other outputs than the lock pins"
            raise ReleaseError(
                f"imports.{alias}: release {entry.name} {entry.version} in domain "
                f"{entry.domain!r} {what}; the metadata store and flow.lock disagree: check "
                "[metadata].url, then run `ebs lock update " + alias + "`"
            )
        rows.append(ReleaseImport(alias=alias, release=found.id))
    return rows
