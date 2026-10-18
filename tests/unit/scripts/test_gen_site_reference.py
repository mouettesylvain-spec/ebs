"""Tests for scripts/gen_site_reference.py: the site's generated reference (task D-01 R3-R5)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import typer.main

import ebs.rules.api
from ebs.cli.main import app

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "gen_site_reference.py"


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_site_reference", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_site_reference"] = module
    spec.loader.exec_module(module)
    return module


def _commands() -> list[tuple[tuple[str, ...], Any]]:
    found: list[tuple[tuple[str, ...], Any]] = []

    def walk(cmd: Any, path: tuple[str, ...]) -> None:
        if getattr(cmd, "hidden", False):
            return
        if path and not hasattr(cmd, "commands"):
            found.append((path, cmd))
        for name, sub in getattr(cmd, "commands", {}).items():
            walk(sub, (*path, name))

    walk(typer.main.get_command(app), ())
    return found


# R3
def test_cli_reference_covers_every_command(gen: ModuleType) -> None:
    pages = gen.render_cli(app)
    commands = _commands()
    assert len(commands) > 15
    for path, cmd in commands:
        heading = f"## `ebs {' '.join(path)}`"
        page = pages[f"{path[0]}.md"]
        assert heading in page, heading
        for param in cmd.params:
            if param.param_type_name == "option" and not param.hidden:
                assert f"`{param.opts[0]}" in page, (path, param.opts)
    assert "build.md" in pages
    assert "index.md" in pages


def test_cli_reference_escapes_html(gen: ModuleType) -> None:
    assert gen.md_text("a <str> | b") == r"a &lt;str&gt; \| b"


# R4
def test_api_reference_lists_rule_api(gen: ModuleType) -> None:
    interfaces = (ROOT / "docs" / "design" / "interfaces.md").read_text(encoding="utf-8")
    page = gen.render_api_page(
        title="Rule plugin API",
        description="x",
        modules=["ebs.rules.api"],
        contract=gen.interfaces_section(interfaces, 7),
    )
    for name in ebs.rules.api.__all__:
        assert f"### `{name}`" in page, name
    assert "class RulePlugin(Protocol):" in page  # the contract section is embedded
    assert "def default_classify(" in page  # signatures are rendered
    assert "Exit 0 ⇒ PASSED" in page  # docstrings are rendered


# R4
def test_api_reference_lists_toolchain_api(gen: ModuleType) -> None:
    import importlib

    interfaces = (ROOT / "docs" / "design" / "interfaces.md").read_text(encoding="utf-8")
    page = gen.render_api_page(
        title="Toolchain API",
        description="x",
        modules=gen.TOOLCHAIN_MODULES,
        contract=gen.interfaces_section(interfaces, 13),
    )
    assert set(gen.TOOLCHAIN_MODULES) >= {"ebs.toolchain.model", "ebs.toolchain.registry"}
    for name in gen.TOOLCHAIN_MODULES:
        module = importlib.import_module(name)
        assert f"## `{name}`" in page
        for symbol in gen.public_names(module):
            assert f"### `{symbol}`" in page, (name, symbol)
    assert "class StaticToolchainResolver" in page  # the § 13 contract is embedded
    assert "def toolchain_id(" in page  # signatures are rendered


def test_rule_reference_covers_registry(gen: ModuleType) -> None:
    assert "ebs.rules.registry" in gen.RULE_MODULES


# R3: the --json sentence is derived from the app, so it cannot claim too much
def test_json_note_names_commands_without_json(gen: ModuleType) -> None:
    root = typer.main.get_command(app)
    without = [
        " ".join(path)
        for path, cmd in _commands()
        if not any("--json" in getattr(p, "opts", ()) for p in cmd.params)
    ]
    note = gen.json_note(root)
    for path in without:
        assert f"`ebs {path}`" in note
    if not without:
        assert note.startswith("Every command takes `--json`")


def test_interfaces_section_bounds(gen: ModuleType) -> None:
    text = "# T\n## 1. One\na\n## 2. Two\nb\n## 3. Three\n"
    assert gen.interfaces_section(text, 2) == "b\n"
    with pytest.raises(ValueError, match="§ 9"):
        gen.interfaces_section(text, 9)


# R5
def test_roadmap_statuses(gen: ModuleType) -> None:
    readme = (
        "## Phase 0 — Foundations (exit: x)\n\n"
        "| ID | Task | Depends on | Wave | Status |\n| --- | --- | --- | --- | --- |\n"
        "| P0-01 | Scaffold | — | 1 | done |\n"
        "| P0-02 | Digests `x` | P0-01 | 2 | todo |\n\n"
        "## Phase 1 — Grid (exit: y)\n\n"
        "| ID | Task | Depends on | Wave | Status |\n| --- | --- | --- | --- | --- |\n"
        "| P1-01 | Slurm | — | 1 | review |\n"
    )
    phases = gen.parse_roadmap(readme)
    assert [p.title for p in phases] == ["Phase 0 — Foundations", "Phase 1 — Grid"]
    assert [(t.id, t.status) for t in phases[0].tasks] == [("P0-01", "done"), ("P0-02", "todo")]
    page = gen.render_roadmap(readme)
    assert "1 of 2 done" in page
    assert "P1-01" in page
    assert "review" in page


def test_roadmap_of_repo_lists_every_task(gen: ModuleType) -> None:
    readme = (ROOT / "docs" / "tasks" / "README.md").read_text(encoding="utf-8")
    ids = {t.id for p in gen.parse_roadmap(readme) for t in p.tasks}
    tasks = (ROOT / "docs" / "tasks").glob("P*-*.md")
    files = {"-".join(p.name.split("-")[:2]) for p in tasks}
    assert files <= ids


def test_config_page_embeds_site_config(gen: ModuleType) -> None:
    overview = (ROOT / "docs" / "design" / "overview.md").read_text(encoding="utf-8")
    page = gen.render_config(overview)
    assert "```toml" in page
    assert "[cas]" in page
    assert "[slurm]" in page


def test_exit_codes_page(gen: ModuleType) -> None:
    page = gen.render_exit_codes()
    assert "| 0 | `OK`" in page
    assert "| 130 | `CANCELLED`" in page
    assert "`FlowError`" in page


def test_generate_writes_every_page(gen: ModuleType, tmp_path: Path) -> None:
    out, public = tmp_path / "reference", tmp_path / "public"
    written = gen.generate(ROOT, out, public)
    names = {p.relative_to(tmp_path).as_posix() for p in written}
    for expected in (
        "reference/cli/index.md",
        "reference/cli/build.md",
        "reference/flow-schema.md",
        "reference/config.md",
        "reference/rule-api.md",
        "reference/toolchain-api.md",
        "reference/exit-codes.md",
        "reference/roadmap.md",
        "public/schemas/flow.v1.schema.json",
    ):
        assert expected in names
    schema = ROOT / "schemas" / "flow.v1.schema.json"
    assert json.loads((public / "schemas" / "flow.v1.schema.json").read_text()) == json.loads(
        schema.read_text()
    )
    for page in out.rglob("*.md"):
        assert page.read_text(encoding="utf-8").startswith("---\ntitle: "), page


def test_generate_removes_stale_pages(gen: ModuleType, tmp_path: Path) -> None:
    out, public = tmp_path / "reference", tmp_path / "public"
    (out / "cli").mkdir(parents=True)
    (out / "cli" / "gone.md").write_text("old")
    gen.generate(ROOT, out, public)
    assert not (out / "cli" / "gone.md").exists()
