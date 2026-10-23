"""Sign-off bundles: a release with everything needed to re-validate it offline (P3-04 R1, R2, R4).

`archive_release` writes one file, `<name>_<version>.signoff.tar.zst` (a tar compressed with zstd),
to a cold-storage directory and records it in the `archives` table. Its members:

- `manifest.json` (first): format, release, flow (path, repository, commit), imported releases,
  toolchain names, and the digest and size of every other member;
- `release.json`, `build.json`, `actions.json` (action rows and their result manifests): the
  records, as the metadata store holds them;
- `attestation.json`: the release's SLSA provenance statement (`ebs.release.attest`), and
  `attestation.intoto.json`, its signed DSSE envelope, when the archiver signs it;
- `plan.json` (the build's plan) and `final-plan.json` (the plan as refined at the end, when the
  build stored one that differs);
- `git/flow.bundle`: a `git bundle` of the flow repository at the recorded commit;
- `imports/<release digest>/release.json` and `plan.json`: the lock closure, the releases the build
  imported and, recursively, the releases those were built from;
- `toolchains/<name>.json`: each toolchain record (module, version, fingerprint, captured env,
  install roots, id);
- `cas/trees/<algo>/<hex>`: the tree manifests of the imported outputs (re-planning matches import
  globs against them), and with `--with-outputs` `cas/blobs/<algo>/<hex>` and the trees of the
  released outputs.

The bytes are deterministic: members sorted (manifest first), mtime 0, uid/gid 0, mode 0644, no
user or time in any member, single-threaded zstd and git packing. Archiving the same release twice
gives the same file. `open_bundle` extracts a bundle and refuses any member the manifest does not
list with the same digest; `restore_bundle` loads one into a CAS and a metadata store (R4).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Final, TypeAlias, TypeVar
from uuid import uuid4

import zstandard
from pydantic import BaseModel, ValidationError

from ebs.cas.api import CAS
from ebs.core.canon import canonical_json
from ebs.core.digest import Digest, hash_bytes, hash_file
from ebs.core.errors import CasError, DigestError, MetadataError, ReleaseError, TreeError
from ebs.core.tree import TreeManifest
from ebs.core.types import JsonValue
from ebs.meta.api import (
    TRANSITIONS,
    ActionRow,
    ArchiveRecord,
    BuildCreate,
    BuildId,
    BuildView,
    MetadataBackend,
    ReleaseCreate,
    ReleaseView,
    ResultManifest,
)
from ebs.plan.planfile import decode
from ebs.plan.types import Plan, PlanToolchain
from ebs.release.attest import import_deps, provenance_statement
from ebs.release.lock import release_digest
from ebs.sources.gitids import run_git
from ebs.toolchain.model import ToolchainResolver

__all__ = [
    "BUNDLE_SUFFIX",
    "FLOW_BUNDLE",
    "FORMAT",
    "MANIFEST",
    "Bundle",
    "Git",
    "GitRunner",
    "Member",
    "archive_release",
    "bundle_file_name",
    "collect_members",
    "open_bundle",
    "recorded_plan",
    "restore_bundle",
    "restore_objects",
    "write_archive",
    "write_bundle",
]

FORMAT: Final = "ebs-signoff-bundle"
FORMAT_VERSION: Final = 1
MANIFEST: Final = "manifest.json"
BUNDLE_SUFFIX: Final = ".signoff.tar.zst"
FLOW_BUNDLE: Final = "git/flow.bundle"
ZSTD_LEVEL: Final = 10
_REF: Final = "refs/ebs/archive/"  # the ref a flow bundle carries: refs/ebs/archive/<commit>

Member: TypeAlias = bytes | Path
"""A member's content: bytes, or a file to copy."""

GitRunner: TypeAlias = Callable[[Sequence[str], Path], bytes]
"""Runs a git argv in a directory and returns its stdout (`ebs.sources.gitids.run_git`)."""


class Git:
    """The git commands a bundle needs, through an injected runner (argv lists, never a shell).

    `bundle` never writes to the source repository: it packs from a scratch repository that
    borrows the source's objects (`objects/info/alternates`)."""

    def __init__(self, run: GitRunner = run_git) -> None:
        self._run = run

    def _git(self, argv: Sequence[str], cwd: Path, what: str) -> bytes:
        try:
            return self._run(["git", *argv], cwd)
        except (OSError, subprocess.CalledProcessError) as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            detail = stderr.decode(errors="replace").strip() or str(exc)
            raise ReleaseError(f"git {what} failed in {cwd}: {detail}") from None

    def toplevel(self, path: Path) -> Path:
        """The top of the working tree holding `path`."""
        out = self._git(["rev-parse", "--show-toplevel"], path, "rev-parse")
        return Path(out.decode().strip())

    def bundle(self, repo: Path, commit: str, dest: Path) -> None:
        """Write a git bundle of `commit` (and its history) of `repo` to `dest`."""
        if not commit or any(c not in "0123456789abcdef" for c in commit):
            raise ReleaseError(f"recorded flow commit {commit!r} is not a git object id")
        try:
            self._run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], repo)
        except (OSError, subprocess.CalledProcessError):
            raise ReleaseError(
                f"the flow repository at {repo} has no commit {commit}; run the archive from a "
                "clone that holds it (git fetch)"
            ) from None
        objects = self._git(["rev-parse", "--git-path", "objects"], repo, "rev-parse")
        objects_dir = Path(os.path.normpath(repo / objects.decode().strip()))
        with tempfile.TemporaryDirectory(prefix="ebs-bundle-") as tmp:
            scratch = Path(tmp)
            self._git(["init", "-q", "--bare"], scratch, "init")
            (scratch / "objects" / "info" / "alternates").write_text(f"{objects_dir}\n")
            self._git(["update-ref", f"{_REF}{commit}", commit], scratch, "update-ref")
            self._git(
                ["-c", "pack.threads=1", "bundle", "create", str(dest.absolute()),
                 f"{_REF}{commit}"],
                scratch, "bundle create",
            )  # fmt: skip

    def checkout(self, bundle: Path, commit: str, dest: Path) -> None:
        """A working tree of `commit` from a flow bundle, at `dest` (which must not exist)."""
        dest.mkdir(parents=True)
        self._git(["init", "-q"], dest, "init")
        self._git(["fetch", "-q", str(bundle.absolute()), f"{_REF}{commit}"], dest, "fetch")
        self._git(
            ["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", commit],
            dest, "checkout",
        )  # fmt: skip


# --- writing -------------------------------------------------------------------------------------


def _json(doc: JsonValue) -> bytes:
    return canonical_json(doc)


def _model(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _check_name(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or str(path) != name
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ReleaseError(f"invalid bundle member name {name!r}")
    return name


def _digest(member: Member) -> tuple[Digest, int]:
    if isinstance(member, bytes):
        return hash_bytes(member), len(member)
    return hash_file(member)


def write_archive(entries: Mapping[str, Member], dest: Path) -> None:
    """Write `entries` as a deterministic tar + zstd file: `manifest.json` first, the others
    sorted by name; every entry a regular file with mtime 0, owner 0:0 and mode 0644."""
    names = sorted(_check_name(n) for n in entries)
    if MANIFEST in names:
        names.remove(MANIFEST)
        names.insert(0, MANIFEST)
    compressor = zstandard.ZstdCompressor(level=ZSTD_LEVEL, threads=0)
    with (
        dest.open("wb") as raw,
        compressor.stream_writer(raw, closefd=False) as zst,
        tarfile.open(fileobj=zst, mode="w|", format=tarfile.PAX_FORMAT) as tar,
    ):
        for name in names:
            member = entries[name]
            info = tarfile.TarInfo(name)
            info.mtime, info.mode, info.uid, info.gid, info.uname, info.gname = (
                0,
                0o644,
                0,
                0,
                "",
                "",
            )
            if isinstance(member, bytes):
                info.size = len(member)
                tar.addfile(info, _BytesReader(member))
            else:
                info.size = member.stat().st_size
                with member.open("rb") as f:
                    tar.addfile(info, f)


class _BytesReader:
    """The `read` that `tarfile.addfile` needs, over bytes."""

    def __init__(self, data: bytes) -> None:
        self._data = memoryview(data)
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        end = len(self._data) if n < 0 else min(len(self._data), self._pos + n)
        chunk = self._data[self._pos : end].tobytes()
        self._pos = end
        return chunk


def write_bundle(
    members: Mapping[str, Member], dest: Path, *, header: Mapping[str, JsonValue]
) -> Digest:
    """Write `members` with a manifest (`header` plus each member's digest and size) to `dest`;
    returns the digest of the file."""
    if MANIFEST in members:
        raise ReleaseError(f"{MANIFEST} is written by write_bundle, not given as a member")
    listed: dict[str, JsonValue] = {}
    for name, member in members.items():
        digest, size = _digest(member)
        listed[_check_name(name)] = {"digest": str(digest), "size": size}
    manifest: dict[str, JsonValue] = {**header, "members": listed}
    write_archive({MANIFEST: _json(manifest), **members}, dest)
    return hash_file(dest)[0]


def bundle_file_name(release: ReleaseView) -> str:
    return f"{release.name.replace('/', '_')}_{release.version}{BUNDLE_SUFFIX}"


def _read(cas: CAS, digest: Digest, what: str) -> bytes:
    try:
        with cas.open(digest) as f:
            return f.read()
    except CasError as exc:
        raise ReleaseError(f"cannot read the {what} {digest}: {exc}") from None


def _blob(cas: CAS, digest: Digest) -> Member:
    path = cas.local_path(digest)
    return path if path is not None else _read(cas, digest, "released blob")


def recorded_plan(store: MetadataBackend, cas: CAS, release: ReleaseView) -> tuple[BuildView, Plan]:
    """The release's build and the plan it recorded last (its final plan when it has one)."""
    build = store.get_build(release.build)
    digest = build.final_plan_digest or build.plan_digest
    return build, decode(_read(cas, digest, "plan of build " + str(build.id)), any_key_schema=True)


def _toolchain(
    store: MetadataBackend, resolver: ToolchainResolver, name: str, tc: PlanToolchain
) -> dict[str, JsonValue]:
    """The record of a planned toolchain: the registered one, else what `resolver` resolves now,
    which must still have the planned id."""
    record = store.get_toolchain(tc.id)
    if record is not None:
        version, fingerprint = record.version, record.fingerprint
        env, roots = dict(record.env), list(record.install_roots)
    else:
        found = resolver.resolve(name, tc.module)
        if found.id != tc.id:
            raise ReleaseError(
                f"toolchain {name!r} ({tc.module}) is not registered and resolves to {found.id} "
                f"now, but the build used {tc.id}; register the toolchain the build used "
                "(ebs toolchain register) and archive again"
            )
        version, fingerprint = found.version, found.fingerprint
        env, roots = dict(found.env), [str(r) for r in found.install_roots]
    return {
        "name": name,
        "module": tc.module,
        "version": version,
        "fingerprint": str(fingerprint),
        "env": dict(sorted(env.items())),
        "install_roots": list(roots),
        "id": str(tc.id),
    }


def _trees(cas: CAS, root: Digest, *, blobs: bool) -> Iterator[tuple[str, Member]]:
    """The manifests of `root` and its subtrees (and with `blobs`, its file contents)."""
    seen: set[Digest] = set()
    stack = [root]
    while stack:
        d = stack.pop()
        if d in seen:
            continue
        seen.add(d)
        manifest = cas.get_tree(d)
        yield f"cas/trees/{d.algo}/{d.hex}", _json(manifest.to_json())
        for entry in manifest.entries:
            if entry.digest is None:
                continue
            if entry.type == "dir":
                stack.append(entry.digest)
            elif blobs:
                yield f"cas/blobs/{entry.digest.algo}/{entry.digest.hex}", _blob(cas, entry.digest)


def collect_members(
    store: MetadataBackend,
    cas: CAS,
    release: ReleaseView,
    *,
    repo: Path,
    git: Git,
    toolchains: ToolchainResolver,
    work: Path,
    with_outputs: bool = False,
    sign: Callable[[dict[str, JsonValue]], str] | None = None,
) -> tuple[dict[str, Member], dict[str, JsonValue]]:
    """The members of `release`'s bundle and its manifest header (`work`: scratch for files)."""
    build, plan = recorded_plan(store, cas, release)
    flow_git = plan.flow.git
    if flow_git is None or not flow_git.commit:
        raise ReleaseError(
            f"build {build.id} of release {release.name} {release.version} was not planned from "
            "a git repository, so its flow cannot be archived; build it from a committed flow"
        )
    if flow_git.dirty:
        raise ReleaseError(
            f"build {build.id} of release {release.name} {release.version} was planned from a "
            "flow with uncommitted changes, which a git bundle cannot reproduce; commit them, "
            "rebuild and release the new build"
        )
    members: dict[str, Member] = {}
    git.bundle(git.toplevel(repo), flow_git.commit, work / "flow.bundle")
    members[FLOW_BUNDLE] = work / "flow.bundle"

    members["release.json"] = _model(release)
    members["build.json"] = _model(build)
    rows = store.list_actions(build.id)
    results: dict[str, JsonValue] = {}
    for row in rows:
        result = store.get_result(build.id, row.action_id)
        if result is not None:
            results[row.action_id] = result.model_dump(mode="json")
    members["actions.json"] = json.dumps(
        {"actions": [r.model_dump(mode="json") for r in rows], "results": results},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()  # fmt: skip
    statement = provenance_statement(release, build, plan, import_deps(store, build.id))
    members["attestation.json"] = _json(statement)
    if sign is not None:
        members["attestation.intoto.json"] = sign(statement).encode()
    members["plan.json"] = _read(cas, build.plan_digest, "plan of build " + str(build.id))
    if build.final_plan_digest is not None and build.final_plan_digest != build.plan_digest:
        members["final-plan.json"] = _read(cas, build.final_plan_digest, "final plan")

    for name, tc in plan.toolchains.items():
        members[f"toolchains/{_check_name(name)}.json"] = _json(
            _toolchain(store, toolchains, name, tc)
        )

    imports: list[dict[str, JsonValue]] = []
    pending = [(build.id, True)]
    visited: set[Digest] = set()
    while pending:
        consumer, direct = pending.pop()
        for imp in store.list_release_imports(consumer):
            imported = store.get_release_by_id(imp.release)
            if imported is None:
                raise ReleaseError(
                    f"build {consumer} imports release id {imp.release}, which cannot be read; "
                    "check your membership of its domain"
                )
            if direct:  # re-planning matches the import's globs against these trees
                for out in imported.outputs.values():
                    if out.type == "tree":
                        members.update(_trees(cas, out.digest, blobs=False))
            digest = release_digest(imported)
            if digest in visited:
                continue
            visited.add(digest)
            folder = f"imports/{digest.hex}"
            up_build, _ = recorded_plan(store, cas, imported)
            members[f"{folder}/release.json"] = _model(imported)
            members[f"{folder}/plan.json"] = _read(
                cas, up_build.final_plan_digest or up_build.plan_digest, "plan of an import"
            )
            imports.append(
                {"name": imported.name, "version": imported.version, "release": str(digest),
                 "dir": folder}
            )  # fmt: skip
            pending.append((imported.build, False))

    if with_outputs:
        for out in release.outputs.values():
            if out.type == "tree":
                members.update(_trees(cas, out.digest, blobs=True))
            else:
                members[f"cas/blobs/{out.digest.algo}/{out.digest.hex}"] = _blob(cas, out.digest)

    header: dict[str, JsonValue] = {
        "format": FORMAT,
        "v": FORMAT_VERSION,
        "release": {
            "domain": release.domain, "name": release.name, "version": release.version,
            "digest": str(release_digest(release)),
        },
        "flow": {"path": plan.flow.path, "repo": flow_git.repo, "commit": flow_git.commit},
        "plan": str(build.final_plan_digest or build.plan_digest),
        "imports": [i for i in sorted(imports, key=lambda i: str(i["dir"]))],
        "toolchains": [name for name in sorted(plan.toolchains)],
        "with_outputs": with_outputs,
    }  # fmt: skip
    return members, header


def archive_release(
    store: MetadataBackend,
    cas: CAS,
    release: ReleaseView,
    *,
    to: Path,
    repo: Path,
    git: Git,
    toolchains: ToolchainResolver,
    by: str,
    with_outputs: bool = False,
    sign: Callable[[dict[str, JsonValue]], str] | None = None,
) -> ArchiveRecord:
    """Write `release`'s sign-off bundle into directory `to` and record it (R2). The flow's git
    commit is read from the repository holding `repo`. An archived file is never replaced: the
    same bytes again only return the existing record; other bytes are refused."""
    to.mkdir(parents=True, exist_ok=True)
    dest = to / bundle_file_name(release)
    with tempfile.TemporaryDirectory(prefix=".ebs-archive-", dir=to) as tmp:
        work = Path(tmp)
        members, header = collect_members(
            store, cas, release, repo=repo, git=git, toolchains=toolchains, work=work,
            with_outputs=with_outputs, sign=sign,
        )  # fmt: skip
        staged = work / dest.name
        digest = write_bundle(members, staged, header=header)
        with staged.open("rb") as f:
            os.fsync(f.fileno())
        staged.chmod(0o444)
        try:
            os.link(staged, dest)  # atomic, and never replaces an archived file
        except FileExistsError:
            if hash_file(dest)[0] != digest:
                raise ReleaseError(
                    f"{dest} already holds a different bundle of {release.name} "
                    f"{release.version}; archived files are never replaced: archive to another "
                    "directory"
                ) from None
    return store.record_archive(release.id, digest, os.path.abspath(dest), by=by)


# --- reading -------------------------------------------------------------------------------------


_M = TypeVar("_M", bound=BaseModel)


@dataclass(frozen=True)
class Bundle:
    """An extracted sign-off bundle whose members all match its manifest."""

    path: Path  # the bundle file
    root: Path  # where its members were extracted
    digest: Digest  # of the bundle file
    manifest: dict[str, JsonValue]

    @property
    def header(self) -> dict[str, JsonValue]:
        """The manifest without its member list (`write_bundle`'s `header`)."""
        return {k: v for k, v in self.manifest.items() if k != "members"}

    def members(self) -> dict[str, Path]:
        listed = self.manifest["members"]
        assert isinstance(listed, dict)
        return {name: self.root / name for name in sorted(listed)}

    def member(self, name: str) -> Path:
        listed = self.manifest["members"]
        assert isinstance(listed, dict)
        if name not in listed:
            raise ReleaseError(f"sign-off bundle {self.path} has no member {name}")
        return self.root / name

    def json(self, name: str) -> JsonValue:
        try:
            doc: JsonValue = json.loads(self.member(name).read_bytes())
        except ValueError as exc:
            raise ReleaseError(
                f"{name} of sign-off bundle {self.path} is not JSON: {exc}"
            ) from None
        return doc

    def _parse(self, name: str, model: type[_M]) -> _M:
        try:
            return model.model_validate_json(self.member(name).read_bytes())
        except ValidationError as exc:
            raise ReleaseError(f"{name} of sign-off bundle {self.path} is invalid: {exc}") from None

    def release(self) -> ReleaseView:
        return self._parse("release.json", ReleaseView)

    def build(self) -> BuildView:
        return self._parse("build.json", BuildView)

    def plan(self) -> Plan:
        """The plan the build recorded last (its final plan when it has one)."""
        listed = self.manifest["members"]
        assert isinstance(listed, dict)
        name = "final-plan.json" if "final-plan.json" in listed else "plan.json"
        return decode(self.member(name).read_bytes(), any_key_schema=True)

    def toolchains(self) -> dict[str, dict[str, JsonValue]]:
        found: dict[str, dict[str, JsonValue]] = {}
        for name in self.members():
            if name.startswith("toolchains/") and name.endswith(".json"):
                doc = self.json(name)
                if not isinstance(doc, dict):
                    raise ReleaseError(f"{name} of sign-off bundle {self.path} is not an object")
                found[name.removeprefix("toolchains/").removesuffix(".json")] = doc
        return found

    def trees(self) -> dict[Digest, TreeManifest]:
        """The tree manifests the bundle holds, by digest."""
        trees: dict[Digest, TreeManifest] = {}
        for name, path in self.members().items():
            if not name.startswith("cas/trees/"):
                continue
            digest = _object_digest(name)
            try:
                manifest = TreeManifest.from_json(json.loads(path.read_bytes()))
            except (ValueError, TreeError) as exc:
                raise ReleaseError(f"{name} of sign-off bundle {self.path}: {exc}") from None
            if manifest.digest(digest.algo) != digest:
                raise ReleaseError(f"{name} of sign-off bundle {self.path} is not tree {digest}")
            trees[digest] = manifest
        return trees


def _object_digest(name: str) -> Digest:
    _, _, algo, hex_ = name.split("/")
    try:
        return Digest.parse(f"{algo}:{hex_}")
    except DigestError as exc:
        raise ReleaseError(f"bundle member {name}: {exc}") from None


def _copy(src: IO[bytes], path: Path) -> None:
    with path.open("xb") as dst:
        shutil.copyfileobj(src, dst)


def open_bundle(path: Path, dest: Path) -> Bundle:
    """Extract the bundle at `path` into `dest` (which must not exist) and check every member
    against the manifest: ReleaseError on any member missing, unlisted or different."""
    digest = hash_file(path)[0]
    dest.mkdir(parents=True)
    seen: set[str] = set()
    try:
        with (
            path.open("rb") as raw,
            zstandard.ZstdDecompressor().stream_reader(raw) as zst,
            tarfile.open(fileobj=zst, mode="r|") as tar,
        ):
            for info in tar:
                name = _check_name(info.name)
                if not info.isfile() or name in seen:
                    raise ReleaseError(f"member {name!r} is not a single regular file")
                seen.add(name)
                src = tar.extractfile(info)
                assert src is not None  # a regular file
                target = dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                _copy(src, target)
    except (tarfile.TarError, zstandard.ZstdError, ReleaseError, OSError) as exc:
        raise ReleaseError(f"{path} is not a readable sign-off bundle: {exc}") from None
    if MANIFEST not in seen:
        raise ReleaseError(f"{path} is not a sign-off bundle: it has no {MANIFEST}")
    try:
        manifest = json.loads((dest / MANIFEST).read_bytes())
    except ValueError as exc:
        raise ReleaseError(f"{MANIFEST} of {path} is not JSON: {exc}") from None
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != FORMAT
        or not isinstance(manifest.get("members"), dict)
    ):
        raise ReleaseError(f"{path} is not a sign-off bundle ({MANIFEST} has no {FORMAT} format)")
    if manifest.get("v") != FORMAT_VERSION:
        raise ReleaseError(
            f"{path} is a version {manifest.get('v')!r} sign-off bundle; this ebs reads version "
            f"{FORMAT_VERSION}"
        )
    listed: dict[str, JsonValue] = manifest["members"]
    for name in sorted(seen - {MANIFEST} - set(listed)):
        raise ReleaseError(f"bundle {path}: member {name} is not listed in its manifest")
    for name in sorted(set(listed) - seen):
        raise ReleaseError(f"bundle {path}: member {name} listed in its manifest is missing")
    for name, entry in sorted(listed.items()):
        actual, size = _digest(dest / name)
        if entry != {"digest": str(actual), "size": size}:
            raise ReleaseError(
                f"bundle {path}: member {name} does not match the digest its manifest records; "
                "the bundle was altered or damaged"
            )
    return Bundle(path=path, root=dest, digest=digest, manifest=manifest)


# --- restoring (R4) ------------------------------------------------------------------------------


def restore_objects(bundle: Bundle, cas: CAS) -> None:
    """Put the bundle's plans and objects into `cas`. A tree is stored only when every object it
    references is available (bundled or already present); imported outputs usually are not."""
    build = bundle.build()
    members = bundle.members()
    for name, expected in (("plan.json", build.plan_digest),
                           ("final-plan.json", build.final_plan_digest)):  # fmt: skip
        if name in members and expected is not None:
            cas.put_file(members[name], expected=expected)
    for name, path in members.items():
        if name.startswith("cas/blobs/"):
            cas.put_file(path, expected=_object_digest(name))
    trees = bundle.trees()
    stored: dict[Digest, bool] = {}

    def store(d: Digest) -> bool:
        if d not in stored:
            stored[d] = False  # a cycle never completes
            manifest = trees[d]
            for entry in manifest.entries:
                if entry.digest is None:
                    continue
                if entry.type == "dir":
                    ok = cas.has(entry.digest) or (entry.digest in trees and store(entry.digest))
                else:
                    ok = cas.has(entry.digest)
                if not ok:
                    return False
            cas.put_manifest(manifest)
            stored[d] = True
        return stored[d]

    for d in trees:
        store(d)


def _restore_build(store: MetadataBackend, bundle: Bundle) -> BuildId:
    """Record the bundled build again: its actions, moved to their recorded states with their
    keys and results, and its final status and plan."""
    build = bundle.build()
    doc = bundle.json("actions.json")
    actions = doc.get("actions") if isinstance(doc, dict) else None
    raw = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(actions, list) or not isinstance(raw, dict):
        raise ReleaseError(f"actions.json of sign-off bundle {bundle.path} lacks actions/results")
    try:  # through JSON: the strict models parse timestamps from JSON only
        rows = [ActionRow.model_validate_json(json.dumps(r)) for r in actions]
        results = {a: ResultManifest.model_validate_json(json.dumps(r)) for a, r in raw.items()}
    except ValidationError as exc:
        raise ReleaseError(f"actions.json of sign-off bundle {bundle.path}: {exc}") from None
    spec = BuildCreate.model_validate(build.model_dump(include=set(BuildCreate.model_fields)))
    try:
        created = store.create_build(spec)
    except MetadataError as exc:
        if "already exists" not in str(exc):
            raise
        # An earlier restore stopped after recording the build (stores cannot look builds up
        # by UUID): record it again under a new UUID; the bundle keeps the original one.
        created = store.create_build(spec.model_copy(update={"uuid": uuid4()}))
    store.add_actions(
        created,
        [r.model_copy(update={"state": "queued", "attempts": 0, "key": None, "queued_at": None,
                              "started_at": None, "finished_at": None, "slurm_job_id": None,
                              "pending_reason": None, "infra_reason": None, "cached": False,
                              "result_key": None})
         for r in rows],
    )  # fmt: skip
    for row in rows:
        result = results.get(row.action_id)
        if row.state in ("done", "failed"):
            store.set_action_state(created, row.action_id, "running", key=row.key)
        elif row.state in TRANSITIONS["queued"]:
            store.set_action_state(created, row.action_id, row.state, key=row.key)
        if result is not None:
            store.record_result(created, row.action_id, result)
        if row.state in ("done", "failed"):
            store.set_action_state(created, row.action_id, row.state)
    if build.status != "running":
        store.finish_build(created, build.status, final_plan=build.final_plan_digest)
    return created


def restore_bundle(
    bundle: Bundle, store: MetadataBackend, cas: CAS, *, location: str, by: str
) -> ReleaseView:
    """Load a bundle into `cas` (its plans and objects) and `store` (its build, actions, results
    and release, and an `archives` row pointing at `location`), so that `ebs verify NAME VERSION`
    works there (R4). A release already present must have the same outputs; it is kept.

    A bundle made without outputs restores records whose released bytes are not in `cas`. The
    build's `release_imports` rows are not restored; the lock closure stays in the bundle."""
    release = bundle.release()
    if cas.domain != release.domain:
        raise ReleaseError(
            f"bundle {bundle.path} holds a release of domain {release.domain!r}; restore it into "
            f"that domain's CAS, not {cas.domain!r}"
        )
    restore_objects(bundle, cas)
    found = store.get_release(release.domain, release.name, release.version)
    if found is None:
        build = _restore_build(store, bundle)
        spec = ReleaseCreate.model_validate(
            {**release.model_dump(include=set(ReleaseCreate.model_fields)), "build": build}
        )
        found = store.create_release(spec, by=release.created_by)
    elif found.outputs != release.outputs:
        raise ReleaseError(
            f"release {release.name} {release.version} already exists in domain "
            f"{release.domain!r} with other outputs than bundle {bundle.path} holds; releases "
            "are immutable, so it cannot be restored here"
        )
    store.record_archive(found.id, bundle.digest, location, by=by)
    return found
