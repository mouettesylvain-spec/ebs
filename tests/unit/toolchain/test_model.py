"""Toolchain model and the YAML-backed StaticToolchainResolver used by P0 plans and tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import ToolchainError
from ebs.toolchain.fingerprint import default_content_hash, fingerprint_roots
from ebs.toolchain.model import (
    StaticToolchainResolver,
    Toolchain,
    ToolchainResolver,
    toolchain_id,
)

FP = hash_bytes(b"tree")


def _tool_tree(base: Path) -> Path:
    root = base / "tools" / "fakesim"
    (root / "bin").mkdir(parents=True)
    exe = root / "bin" / "fakesim"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return root


def _config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".ebs" / "toolchains.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_from_parts_computes_id() -> None:
    tc = Toolchain.from_parts(
        name="sim",
        module="fakesim/1.0",
        version="1.0",
        install_roots=(Path("/opt/x"),),
        env={"B": "2", "A": "1"},
        fingerprint=FP,
    )
    assert tc.id == toolchain_id("fakesim/1.0", "1.0", FP, {"A": "1", "B": "2"})
    assert list(tc.env) == ["A", "B"]
    with pytest.raises(TypeError):
        tc.env["C"] = "3"  # type: ignore[index]
    assert hash(tc) == hash(
        Toolchain.from_parts(
            name="sim",
            module="fakesim/1.0",
            version="1.0",
            install_roots=(Path("/opt/x"),),
            env={"A": "1", "B": "2"},
            fingerprint=FP,
        )
    )


def test_static_resolver_fingerprints_relative_roots(tmp_path: Path) -> None:
    root = _tool_tree(tmp_path)
    path = _config(
        tmp_path,
        """\
version: 1
toolchains:
  fakesim/1.0:
    install_roots: [../tools/fakesim]
    env: {PATH: "/opt/fakesim/bin:/usr/bin:/bin", FAKESIM_HOME: /opt/fakesim}
""",
    )
    resolver: ToolchainResolver = StaticToolchainResolver(path)
    tc = resolver.resolve("sim", "fakesim/1.0")
    assert tc.name == "sim"
    assert tc.module == "fakesim/1.0"
    assert tc.version == "1.0"  # derived from the module name
    assert tc.install_roots == (root,)
    assert tc.fingerprint == fingerprint_roots([root], content_hash=default_content_hash)
    assert tc.id == toolchain_id("fakesim/1.0", "1.0", tc.fingerprint, dict(tc.env))
    # Same module under another flow-level name: same id.
    assert resolver.resolve("other", "fakesim/1.0").id == tc.id


def test_static_resolver_caches_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tool_tree(tmp_path)
    path = _config(
        tmp_path, "version: 1\ntoolchains:\n  fakesim/1.0: {install_roots: [../tools/fakesim]}\n"
    )
    resolver = StaticToolchainResolver(path)
    first = resolver.resolve("sim", "fakesim/1.0")
    monkeypatch.setattr(os, "scandir", None)  # a second walk would crash
    assert resolver.resolve("sim", "fakesim/1.0").id == first.id


def test_static_resolver_pinned_fingerprint_and_version(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        f"""\
version: 1
toolchains:
  fakesim:
    version: "2.1"
    install_roots: [/nonexistent/fakesim]
    fingerprint: "{FP}"
""",
    )
    tc = StaticToolchainResolver(path).resolve("sim", "fakesim")
    assert tc.version == "2.1"
    assert tc.fingerprint == FP  # roots not walked when pinned
    assert tc.env == {}


def test_static_resolver_unknown_module(tmp_path: Path) -> None:
    path = _config(tmp_path, f"version: 1\ntoolchains:\n  a/1: {{fingerprint: '{FP}'}}\n")
    with pytest.raises(ToolchainError, match=r"questa/2025\.2.*toolchains\.yaml.*a/1"):
        StaticToolchainResolver(path).resolve("questa", "questa/2025.2")


def test_static_resolver_missing_version(tmp_path: Path) -> None:
    path = _config(tmp_path, f"version: 1\ntoolchains:\n  noversion: {{fingerprint: '{FP}'}}\n")
    with pytest.raises(ToolchainError, match="version"):
        StaticToolchainResolver(path).resolve("x", "noversion")


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("toolchains: {}\n", "version"),
        ("version: 1\ntoolchains:\n  a/1: {bogus: 1}\n", "bogus"),
        ("version: 1\ntoolchains:\n  a/1: {fingerprint: nope}\n", "fingerprint"),
        ("version: 1\ntoolchains:\n  a/1: {env: {A: 1}}\n", "env"),
        ("version: 1\ntoolchains: [\n", "YAML"),
        ("version: 1\ntoolchains:\n  a/1: {install_roots: []}\n", "install_roots or fingerprint"),
    ],
)
def test_static_resolver_invalid_file(tmp_path: Path, text: str, match: str) -> None:
    path = _config(tmp_path, text)
    with pytest.raises(ToolchainError, match=match) as info:
        StaticToolchainResolver(path)
    assert str(path) in str(info.value)


def test_static_resolver_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ToolchainError, match=r"toolchains\.yaml"):
        StaticToolchainResolver(tmp_path / ".ebs" / "toolchains.yaml")


def test_fingerprint_digest_type() -> None:
    assert isinstance(FP, Digest)
