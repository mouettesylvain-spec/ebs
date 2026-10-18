"""The shipped editor and CI integration files are valid and call real commands (task D-01 R7)."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import typer.main
from ruamel.yaml import YAML

from ebs.cli.main import app

ROOT = Path(__file__).resolve().parents[3]
INTEGRATIONS = ROOT / "integrations"
FILES = sorted(p for p in INTEGRATIONS.rglob("*") if p.is_file() and p.name != "README.md")
# `ebs <group> <command>` or `ebs <command>` on one line, in a shell line or an argv list.
EBS_CALL = re.compile(
    r"""(?<![\w./$(-])ebs["']?(?:[ \t]*,[ \t]*["']|[ \t]+)([a-z][a-z-]*)"""
    r"""(?:["']?(?:[ \t]*,[ \t]*["']|[ \t]+)([a-z][a-z-]*))?"""
)


def command_paths() -> set[tuple[str, ...]]:
    root = typer.main.get_command(app)
    paths: set[tuple[str, ...]] = set()

    def walk(cmd: Any, prefix: tuple[str, ...]) -> None:
        paths.add(prefix)
        for name, sub in getattr(cmd, "commands", {}).items():
            walk(sub, (*prefix, name))

    walk(root, ())
    return paths


def ebs_calls(text: str) -> list[tuple[str, ...]]:
    return [tuple(part for part in m.groups() if part) for m in EBS_CALL.finditer(text)]


def test_ebs_calls_parse() -> None:
    assert ebs_calls('run: ebs build --cache write\n"command": "ebs logs ${input:action}"') == [
        ("build",),
        ("logs",),
    ]
    assert ebs_calls("ebs toolchain register questa/2025.2") == [("toolchain", "register")]
    assert ebs_calls("pip install ebs-runner ./ebs.mk") == []
    assert ebs_calls("EBS ?= ebs\nall: build") == []  # never across lines


def test_command_paths_know_groups() -> None:
    known = command_paths()
    assert ("toolchain", "register") in known
    assert ("build",) in known
    assert ("toolchain", "bogus") not in known


def test_integrations_exist() -> None:
    names = {p.relative_to(INTEGRATIONS).as_posix() for p in FILES}
    for expected in (
        "vscode/settings.json",
        "vscode/tasks.json",
        "vscode/extensions.json",
        "jetbrains/jsonSchemas.xml",
        "nvim/ebs.lua",
        "ci/github-actions.yml",
        "ci/gitlab-ci.yml",
        "make/ebs.mk",
    ):
        assert expected in names


# R7
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.relative_to(INTEGRATIONS).as_posix())
def test_integration_file_parses(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        json.loads(text)
    elif path.suffix in {".yml", ".yaml"}:
        YAML(typ="safe", pure=True).load(text)
    elif path.suffix == ".xml":
        ET.fromstring(text)


# R7
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.relative_to(INTEGRATIONS).as_posix())
def test_integration_commands_exist(path: Path) -> None:
    known = command_paths()
    groups = {p[:-1] for p in known if p}
    for call in ebs_calls(path.read_text(encoding="utf-8")):
        shown = f"{path.name}: `ebs {' '.join(call)}`"
        assert call[:1] in known, shown
        if call[:1] in groups:  # `ebs toolchain register`; else the 2nd word is an argument
            assert call[:2] in known, shown


def test_vscode_schema_points_at_site() -> None:
    settings = json.loads((INTEGRATIONS / "vscode" / "settings.json").read_text(encoding="utf-8"))
    urls = list(settings["yaml.schemas"])
    assert any(url.endswith("/schemas/flow.v1.schema.json") for url in urls), urls
