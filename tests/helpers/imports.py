"""Releases whose outputs are real CAS objects, for import and `flow.lock` tests (P2-03).

`publish` stores each output (a dict of files is a tree, bytes a file) in the CAS, records a
finished build that produced them, releases them and optionally points a channel at the release.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.meta.api import OutputResult, ReleaseView
from ebs.release.releases import ReleaseBackend, create_release
from tests.helpers.release import Action, finish

Content = Mapping[str, str] | bytes
LINK = "@link:"  # a tree value "@link:a.sv" is a symlink to a.sv

SRCS: dict[str, str] = {
    "rtl/a.sv": "module a; endmodule\n",
    "rtl/b.sv": "module b; endmodule\n",
    "rtl/sub/c.sv": "module c; endmodule\n",
    "rtl/.hidden.sv": "module hidden; endmodule\n",
    "rtl/files.f": "rtl/a.sv\nrtl/b.sv\n",
    "doc/readme.md": "rtl sources\n",
}


def store_output(cas: FsCAS, tmp: Path, content: Content, *, deterministic: bool) -> OutputResult:
    if isinstance(content, bytes):
        digest = cas.put_bytes(content)
        kind, size = "file", len(content)
    else:
        root = tmp / f"tree-{len(list(tmp.glob('tree-*')))}"
        for path, text in content.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            if text.startswith(LINK):
                (root / path).symlink_to(text.removeprefix(LINK))
            else:
                (root / path).write_text(text)
        digest = cas.put_tree(root)
        kind = "tree"
        size = sum(len(t.encode()) for t in content.values() if not t.startswith(LINK))
    oid = digest if deterministic else hash_bytes(b"nd:" + str(digest).encode())
    return OutputResult(digest=digest, id=oid, type=kind, size=size)  # type: ignore[arg-type]


def publish(
    store: ReleaseBackend,
    cas: FsCAS,
    tmp: Path,
    outputs: Mapping[str, Content],
    *,
    name: str = "rtl-team/cpu",
    version: str = "1",
    nondeterministic: frozenset[str] = frozenset(),
    channel: str | None = None,
) -> ReleaseView:
    """Release `outputs` ({"step.output": content}) of a new build in `cas.domain`."""
    tmp.mkdir(parents=True, exist_ok=True)
    actions: dict[str, dict[str, OutputResult]] = {}
    for ref, content in outputs.items():
        step, output = ref.split(".", 1)
        actions.setdefault(step, {})[output] = store_output(
            cas, tmp, content, deterministic=ref not in nondeterministic
        )
    build = finish(
        store, {step: Action(outs) for step, outs in actions.items()}, domain=cas.domain,
        project="rtl",
    )  # fmt: skip
    selections = []  # `compile[lib=a].worklib` is selected as `compile.worklib[lib=a]`
    for ref in outputs:
        step, output = ref.split(".", 1)
        base, _, row = step.partition("[")
        selections.append(f"{base}.{output}[{row}" if row else ref)
    release = create_release(
        store, name=name, version=version, build=build, outputs=selections, by="alice"
    )
    if channel is not None:
        store.set_channel(cas.domain, name, channel, release.id, by="alice")
    return release
