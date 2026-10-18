"""Flow snippets on the documentation site are valid flows (task D-01 R2).

A fenced block written as ```yaml title="…flow.yaml"``` (or `.yml`) is a complete flow file: it
must pass the committed JSON Schema and the flow model. Excerpts of a flow carry no such title.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest
from ruamel.yaml import YAML

from ebs.flow.loader import load_flow

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "site" / "src" / "content" / "docs"
SCHEMA = ROOT / "schemas" / "flow.v1.schema.json"
# A fence may be indented (inside a list item); its lines share that indentation.
FLOW_BLOCK = re.compile(
    r'^([ \t]*)```ya?ml[^\n]*\btitle="([^"]*flow\.ya?ml)"[^\n]*\n(.*?)^\1```',
    re.MULTILINE | re.DOTALL,
)


def flow_snippets(text: str) -> list[tuple[str, str]]:
    """(title, body) of every complete-flow block in a page."""
    found = []
    for m in FLOW_BLOCK.finditer(text):
        indent, title, body = m[1], m[2], m[3]
        found.append((title, "".join(line.removeprefix(indent) for line in body.splitlines(True))))
    return found


def site_snippets() -> list[tuple[str, str, str]]:
    found = []
    for page in sorted(DOCS.rglob("*.md*")):
        if page.suffix not in {".md", ".mdx"}:
            continue
        rel = page.relative_to(DOCS).as_posix()
        for i, (title, body) in enumerate(flow_snippets(page.read_text(encoding="utf-8"))):
            found.append((f"{rel}#{i}:{title}", title, body))
    return found


def test_flow_snippets_parse() -> None:
    text = 'x\n```yaml title="a/flow.yaml"\nversion: 1\n```\n```yaml\nno: title\n```\n'
    assert flow_snippets(text) == [("a/flow.yaml", "version: 1\n")]
    nested = '1. x\n\n   ```yaml title="b/flow.yaml"\n   version: 1\n   steps: {}\n   ```\n'
    assert flow_snippets(nested) == [("b/flow.yaml", "version: 1\nsteps: {}\n")]


def test_site_has_flow_snippets() -> None:
    # The recipes and tutorials pages hold several; zero would mean the pattern broke.
    assert len(site_snippets()) >= 3


# R2
@pytest.mark.parametrize(("where", "title", "body"), site_snippets(), ids=lambda v: str(v)[:60])
def test_flow_snippets_validate(where: str, title: str, body: str, tmp_path: Path) -> None:
    path = tmp_path / Path(title).name
    path.write_text(body, encoding="utf-8")
    load_flow(path)  # FlowError names the line of the snippet
    data = json.loads(json.dumps(YAML(typ="safe", pure=True).load(body)))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(data, schema, cls=jsonschema.Draft202012Validator)
