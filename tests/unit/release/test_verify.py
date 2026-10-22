"""`ebs verify`: re-plan a sign-off bundle and compare every action key (P3-04 R3)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from ebs.core.digest import hash_bytes
from ebs.core.errors import ExitCode
from ebs.plan.keys import with_key
from ebs.plan.planfile import encode
from ebs.plan.types import SourceInput
from ebs.release.archive import Bundle, archive_release, open_bundle, write_bundle
from ebs.release.verify import verify_bundle
from ebs.rules.api import RuleSettings
from ebs.rules.registry import RuleRegistry
from ebs.rules.shell import ShellRule
from tests.helpers.cli import Site, TestServices
from tests.helpers.signoff import SignedOff, hermetic_git, signed_off


def _bundle(s: SignedOff, tmp_path: Path) -> Bundle:
    rec = archive_release(
        s.services.memory_store, s.cas, s.release, to=tmp_path / "cold", repo=s.site.proj,
        git=hermetic_git(), toolchains=s.services.toolchains(s.site.proj), by="alice",
    )  # fmt: skip
    return open_bundle(Path(rec.location), tmp_path / "open")


# R3: re-planned from the bundle alone (flow from the git bundle, toolchains from the records,
# imported trees from the bundle), every action key equals the recorded one.
def test_keys_match(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    bundle = _bundle(s, tmp_path)
    recorded = bundle.plan()
    assert all(a.key is not None for a in recorded.actions)
    report = verify_bundle(bundle, rules=s.services.rules(), git=hermetic_git(),
                           work=tmp_path / "work")  # fmt: skip
    assert report.ok
    assert report.mismatches == ()
    assert report.actions == report.matched == len(recorded.actions) == 3
    assert report.release == "verif/cpu 2026.10.1"

    # The CLI, by bundle path, needs neither the metadata store nor the live CAS.
    (tmp_path / "cas").rename(tmp_path / "cas-gone")
    bare = Site(tmp_path, TestServices(tmp_path, with_store=False))
    by_path = bare.invoke(["verify", str(bundle.path), "--json"])
    assert by_path.exit_code == ExitCode.OK, by_path.output
    doc = json.loads(by_path.stdout)
    assert (doc["ok"], doc["actions"], doc["matched"], doc["mismatches"]) == (True, 3, 3, [])
    (tmp_path / "cas-gone").rename(tmp_path / "cas")

    # By release name: its latest archive, checked against the recorded bundle digest.
    by_name = s.site.invoke(["verify", "verif/cpu", "2026.10.1", "--domain", "test"])
    assert by_name.exit_code == ExitCode.OK, by_name.output
    assert "3/3 action keys match" in by_name.stdout
    bundle.path.chmod(0o644)  # archived files are read-only
    bundle.path.write_bytes(bundle.path.read_bytes() + b"\0")
    changed = s.site.invoke(["verify", "verif/cpu", "2026.10.1", "--domain", "test"])
    assert changed.exit_code == ExitCode.USAGE
    assert "does not match" in changed.output
    none = s.site.invoke(["verify", "rtl-team/cpu", "1", "--domain", "test"])
    assert none.exit_code == ExitCode.USAGE
    assert "ebs release archive rtl-team/cpu 1" in none.output


class _ShellV2(ShellRule):
    version = "2"


# R3: a mismatch names the action and the key-document fields that differ (`diff_plans`).
def test_mismatch_reasons(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    bundle = _bundle(s, tmp_path)

    # Another implementation of the rule: every action's `rule` field differs.
    rules = RuleRegistry([_ShellV2(RuleSettings())])
    report = verify_bundle(bundle, rules=rules, git=hermetic_git(), work=tmp_path / "w1")
    assert not report.ok
    assert report.matched == 0
    assert {(d.action_id, d.status, d.changed) for d in report.mismatches} == {
        ("lint", "changed", ("rule",)),
        ("sim[test=smoke]", "changed", ("rule",)),
        ("sim[test=random]", "changed", ("rule",)),
    }

    # The recorded build hashed other bytes than the commit holds for one source: only that
    # input of that action differs (its consumers got the recorded output ids).
    recorded = bundle.plan()
    lint = next(a for a in recorded.actions if a.action_id == "lint")
    src = next(r for r in lint.inputs if isinstance(r.source, SourceInput))
    other = dataclasses.replace(src, id=hash_bytes(b"other bytes"))
    tampered = with_key(dataclasses.replace(
        lint, inputs=tuple(other if r is src else r for r in lint.inputs)))  # fmt: skip
    plan = dataclasses.replace(
        recorded, actions=tuple(tampered if a is lint else a for a in recorded.actions)
    )
    members: dict[str, bytes | Path] = {**bundle.members(), "final-plan.json": encode(plan)}
    write_bundle(members, tmp_path / "tampered.tar.zst", header=bundle.header)
    edited = open_bundle(tmp_path / "tampered.tar.zst", tmp_path / "open-2")
    report = verify_bundle(edited, rules=s.services.rules(), git=hermetic_git(),
                           work=tmp_path / "w2")  # fmt: skip
    assert [(d.action_id, d.changed) for d in report.mismatches] == [
        ("lint", (f'inputs["{src.logical_path}"]',))
    ]
    assert report.matched == 2

    shown = s.site.invoke(["verify", str(edited.path)])
    assert shown.exit_code == ExitCode.ACTIONS_FAILED
    assert "2/3 action keys match" in shown.stdout
    assert f'lint: changed: inputs["{src.logical_path}"]' in shown.stdout
