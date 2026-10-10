#!/usr/bin/env python3
"""Capture anonymized `squeue --json` / `sacct --json` fixtures from a real SLURM cluster.

Usage, on a login node, after submitting a few jobs that cover the states you need (running,
pending on licenses, an array with a throttle, an OOM, a timeout, a cancel…):

    uv run python scripts/capture_slurm_fixtures.py --out tests/fixtures/slurm/26.05 \\
        --prefix mixed 1234 1240_3 1250

writes `squeue_<prefix>.json` and `sacct_<prefix>.json` with:

- users, groups, accounts, partitions, QOS, cluster → u1…, g1…, a1…, p1…, q1…, c1;
- node names (also inside hostlists and reasons) → n001…; license servers → flexlm;
- job names → job1…; paths → /work/u1/build[/fN]; numeric uid/gid → 1001…;
- free text (comments, wckeys, submit lines, scripts) → "".

Then every remaining string is scanned for the original names. Review the diff anyway before
committing (CLAUDE.md: no real hostnames, license servers or project names in the repo), and
update the expectations in tests/unit/exec/slurm/test_cli.py::test_parse_fixtures.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

_CATEGORY_KEYS: dict[str, str] = {
    "user_name": "user",
    "user": "user",
    "kill_request_user": "user",
    "group_name": "group",
    "group": "group",
    "account": "account",
    "partition": "partition",
    "qos": "qos",
    "cluster": "cluster",
}
_NODE_KEYS = {"nodes", "batch_host", "allocating_node", "failed_node", "range", "list"}
_FILE_KEYS = {
    "command",
    "standard_output",
    "standard_error",
    "standard_input",
    "stdout_expanded",
    "stderr_expanded",
    "stdin_expanded",
    "stdout",
    "stderr",
    "stdin",
}
_DIR_KEYS = {"current_working_directory", "working_directory"}
_ID_KEYS = {"user_id": "user", "group_id": "group"}
_BLANK_KEYS = {
    "comment",
    "admin_comment",
    "system_comment",
    "extra",
    "wckey",
    "submit_line",
    "script",
    "container",
    "container_id",
    "mcs_label",
    "label",
    "administrator",
    "job",
    "system",
    "source",
}
_PREFIX = {
    "user": "u",
    "group": "g",
    "account": "a",
    "partition": "p",
    "qos": "q",
    "cluster": "c",
    "job": "job",
}
_HOSTLIST = re.compile(r"^([^\[\],]*)\[([0-9,\-]+)\](.*)$")


def expand_hostlist(text: str) -> list[str]:
    """ "n[01-03,7]x,login1" → ["n01x", "n02x", "n03x", "n7x", "login1"] (one bracket level)."""
    names: list[str] = []
    for part in re.split(r",(?![^\[]*\])", text):
        m = _HOSTLIST.match(part)
        if m is None:
            if part:
                names.append(part)
            continue
        head, ranges, tail = m.groups()
        for r in ranges.split(","):
            lo, _, hi = r.partition("-")
            width = len(lo)
            for i in range(int(lo), int(hi or lo) + 1):
                names.append(f"{head}{i:0{width}d}{tail}")
    return names


class Anonymizer:
    """Consistent replacement of identifying values; one instance per capture."""

    def __init__(self) -> None:
        self.maps: dict[str, dict[str, str]] = {}
        self.ids: dict[str, dict[int, int]] = {}
        self.nodes: dict[str, str] = {}
        self.servers: dict[str, str] = {}
        self.files: dict[str, str] = {}

    def _alias(self, category: str, value: str) -> str:
        table = self.maps.setdefault(category, {})
        if value not in table:
            table[value] = f"{_PREFIX[category]}{len(table) + 1}"
        return table[value]

    def node(self, name: str) -> str:
        if name not in self.nodes:
            self.nodes[name] = f"n{len(self.nodes) + 1:03d}"
        return self.nodes[name]

    def _nodes(self, text: str) -> str:
        if text in ("", "None assigned", "(null)"):
            return text
        return ",".join(self.node(n) for n in expand_hostlist(text))

    def _licenses(self, text: str) -> str:
        parts = []
        for item in text.split(","):
            feature, at, rest = item.partition("@")
            if not at:
                parts.append(item)
                continue
            server, colon, count = rest.partition(":")
            if server not in self.servers:
                n = len(self.servers)
                self.servers[server] = "flexlm" if n == 0 else f"flexlm{n + 1}"
            parts.append(f"{feature}@{self.servers[server]}{colon}{count}")
        return ",".join(parts)

    def _path(self, value: str, *, is_dir: bool) -> str:
        if not value or not value.startswith("/") or value == "/dev/null":
            return value
        if is_dir:
            return "/work/u1/build"
        if value not in self.files:
            suffix = Path(value).suffix
            self.files[value] = f"/work/u1/build/f{len(self.files) + 1}{suffix}"
        return self.files[value]

    def _key(self, key: str, value: Any) -> Any:
        if isinstance(value, str):
            if key in _BLANK_KEYS:
                return ""
            if key in _CATEGORY_KEYS and value:
                return self._alias(_CATEGORY_KEYS[key], value)
            if key in _NODE_KEYS:
                return self._nodes(value)
            if key == "licenses" or key.endswith("_str"):
                return self._licenses(value)
            if key in _FILE_KEYS:
                return self._path(value, is_dir=False)
            if key in _DIR_KEYS:
                return self._path(value, is_dir=True)
            return value
        if isinstance(value, int) and not isinstance(value, bool) and key in _ID_KEYS:
            table = self.ids.setdefault(_ID_KEYS[key], {})
            return table.setdefault(value, 1000 + len(table) + 1)
        if key in _NODE_KEYS and isinstance(value, list):
            return [self._nodes(v) if isinstance(v, str) else v for v in value]
        return self._walk(value)

    def _walk(self, value: Any) -> Any:
        if isinstance(value, dict):
            is_job = "job_id" in value  # "name" elsewhere is a signal, step or TRES name
            return {
                k: self._alias("job", v)
                if is_job and k == "name" and isinstance(v, str) and v
                else self._key(k, v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._walk(v) for v in value]
        return value

    def _substitutions(self) -> list[tuple[re.Pattern[str], str]]:
        pairs: dict[str, str] = {}
        for table in self.maps.values():
            pairs.update(table)
        pairs.update(self.nodes)
        pairs.update(self.servers)
        ordered = sorted(pairs.items(), key=lambda kv: -len(kv[0]))
        return [
            (re.compile(rf"(?<![A-Za-z0-9]){re.escape(orig)}(?![A-Za-z0-9])"), alias)
            for orig, alias in ordered
            if orig and orig != alias
        ]

    def _scrub(self, value: Any, subs: list[tuple[re.Pattern[str], str]]) -> Any:
        if isinstance(value, dict):
            return {k: self._scrub(v, subs) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v, subs) for v in value]
        if isinstance(value, str):
            for pattern, alias in subs:
                value = pattern.sub(alias, value)
        return value

    def apply(self, doc: Any) -> Any:
        """Anonymize a whole `--json` document."""
        out = self._walk(doc)
        meta = out.get("meta") if isinstance(out, dict) else None
        if isinstance(meta, dict) and isinstance(meta.get("client"), dict):
            meta["client"]["source"] = "/dev/pts/0"
        return self._scrub(out, self._substitutions())


def anonymize(doc: Any) -> Any:
    return Anonymizer().apply(doc)


def _run(argv: list[str]) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, check=True)
    return done.stdout


def capture(
    jobs: Sequence[str],
    out: Path,
    *,
    prefix: str,
    run: Callable[[list[str]], str] = _run,
) -> None:
    """Run squeue and sacct for `jobs` and write the anonymized documents under `out`."""
    ids = ",".join(jobs)
    anon = Anonymizer()
    out.mkdir(parents=True, exist_ok=True)
    for command, argv in (
        ("squeue", ["squeue", "--json", "--states=all", f"--jobs={ids}"]),
        ("sacct", ["sacct", "--json", f"--jobs={ids}"]),
    ):
        doc = anon.apply(json.loads(run(argv)))
        path = out / f"{command}_{prefix}.json"
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="fixture directory")
    parser.add_argument("--prefix", required=True, help="file name suffix, e.g. 'mixed'")
    parser.add_argument("jobs", nargs="+", help="job ids or <array>_<task> ids")
    args = parser.parse_args(argv)
    capture(args.jobs, args.out, prefix=args.prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
