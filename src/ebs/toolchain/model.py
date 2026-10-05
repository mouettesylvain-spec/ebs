"""Toolchain model, toolchain ids and resolvers (docs/design/interfaces.md § 13).

A toolchain id is the digest of the module name, version, install-tree fingerprint and captured
environment; it is the toolchain's contribution to every action key. The planner (L2) depends on
`ToolchainResolver` only. Phase 0 uses `StaticToolchainResolver`, which reads
`.ebs/toolchains.yaml`; the modulefile-based registry arrives in P1-07.

`.ebs/toolchains.yaml` (keyed by module, so one entry serves every flow-level name)::

    version: 1
    toolchains:
      fakesim/1.0:
        version: "1.0"               # optional: defaults to the text after the last "/"
        install_roots: [../tools/fakesim]   # relative to this file's directory
        env: {PATH: "/opt/fakesim/bin:/usr/bin:/bin"}
        fingerprint: "sha256:…"      # optional: pins the fingerprint instead of walking roots
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ebs.core.canon import digest_json
from ebs.core.digest import Digest
from ebs.core.errors import DigestError, ToolchainError
from ebs.core.types import JsonValue
from ebs.toolchain.fingerprint import default_content_hash, fingerprint_roots


@dataclass(frozen=True, slots=True)
class Toolchain:
    """An immutable, fingerprinted tool environment. Use `from_parts` to compute the id."""

    name: str  # flow-level name (e.g. "questa"); not part of the id
    module: str
    version: str
    install_roots: tuple[Path, ...]
    env: Mapping[str, str] = field(hash=False)  # read-only, sorted by name
    fingerprint: Digest
    id: Digest

    def __post_init__(self) -> None:
        object.__setattr__(self, "install_roots", tuple(self.install_roots))
        object.__setattr__(self, "env", MappingProxyType(dict(sorted(self.env.items()))))

    @classmethod
    def from_parts(
        cls,
        *,
        name: str,
        module: str,
        version: str,
        install_roots: Sequence[Path],
        env: Mapping[str, str],
        fingerprint: Digest,
    ) -> Toolchain:
        return cls(
            name=name,
            module=module,
            version=version,
            install_roots=tuple(install_roots),
            env=env,
            fingerprint=fingerprint,
            id=toolchain_id(module, version, fingerprint, env),
        )


def toolchain_id(module: str, version: str, fingerprint: Digest, env: Mapping[str, str]) -> Digest:
    """`digest_json({"module", "version", "fingerprint", "env"})`, with strings NFC-normalized.

    `env` is expected to be HOME-normalized already (`capture_env` does that). Two names that
    differ only by Unicode normalization are rejected: merging them would give two different
    environments the same id.
    """
    normalized: dict[str, JsonValue] = {_nfc(k): _nfc(v) for k, v in env.items()}
    if len(normalized) != len(env):
        clashes = sorted(k for k in env if _nfc(k) != k)
        raise ToolchainError(
            f"toolchain {module!r} env has variable names that differ only by Unicode "
            f"normalization ({clashes!r}); rename them"
        )
    return digest_json(
        {
            "module": _nfc(module),
            "version": _nfc(version),
            "fingerprint": str(fingerprint),
            "env": normalized,
        }
    )


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


class ToolchainResolver(Protocol):
    def resolve(self, name: str, module: str) -> Toolchain: ...


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictStr | None = None
    install_roots: tuple[StrictStr, ...] = ()
    env: dict[StrictStr, StrictStr] = {}
    fingerprint: StrictStr | None = None

    @field_validator("fingerprint")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                Digest.parse(value)
            except DigestError as exc:
                raise ValueError(str(exc)) from None
        return value


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    toolchains: dict[StrictStr, _Entry]


class StaticToolchainResolver:
    """Resolves toolchains from a YAML file (P0 tests and demos); fingerprints are cached."""

    def __init__(self, path: Path) -> None:
        self.path = path.absolute()
        self._entries = _load(self.path)
        self._cache: dict[str, tuple[str, tuple[Path, ...], Digest]] = {}

    def resolve(self, name: str, module: str) -> Toolchain:
        if module not in self._cache:
            self._cache[module] = self._fingerprint(module)
        version, roots, fingerprint = self._cache[module]
        return Toolchain.from_parts(
            name=name,
            module=module,
            version=version,
            install_roots=roots,
            env=self._entries[module].env,
            fingerprint=fingerprint,
        )

    def _fingerprint(self, module: str) -> tuple[str, tuple[Path, ...], Digest]:
        entry = self._entries.get(module)
        if entry is None:
            known = ", ".join(sorted(self._entries)) or "none"
            raise ToolchainError(
                f"toolchain module {module!r} is not defined in {self.path} "
                f"(defined: {known}); add it under `toolchains:`"
            )
        version = entry.version if entry.version is not None else _version_from(module)
        roots = tuple(
            Path(os.path.normpath(self.path.parent / root)) for root in entry.install_roots
        )
        if entry.fingerprint is not None:
            fingerprint = Digest.parse(entry.fingerprint)
        else:
            fingerprint = fingerprint_roots(roots, content_hash=default_content_hash)
        return version, roots, fingerprint


def _version_from(module: str) -> str:
    return module.rpartition("/")[2]


def _load(path: Path) -> dict[str, _Entry]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolchainError(
            f"cannot read toolchain file {path}: {exc.strerror}; "
            "create it (see ebs.toolchain.model for the format) or pass another path"
        ) from exc
    try:
        data = YAML(typ="safe", pure=True).load(text)
    except YAMLError as exc:
        raise ToolchainError(f"{path}: invalid YAML: {exc}") from None
    try:
        parsed = _File.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ToolchainError(f"{path}: invalid toolchain file: {problems}") from None
    for module, entry in parsed.toolchains.items():
        if not entry.install_roots and entry.fingerprint is None:
            raise ToolchainError(f"{path}: toolchain {module!r} needs install_roots or fingerprint")
        if entry.version is None and "/" not in module:
            raise ToolchainError(
                f"{path}: toolchain {module!r} has no version: set `version:` or name the "
                "module <name>/<version>"
            )
    return dict(parsed.toolchains)
