from __future__ import annotations

from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.runner.main import EXIT_OK, EXIT_USAGE, EXIT_VERIFY, RunRequest, main
from tests.helpers.runner import Harness, shell_spec, store_plan


# R1
def test_unknown_action_exit_64(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    code, _build = h.run(shell_spec("true", action_id="lint"), action_id="lint[typo]")
    assert code == EXIT_USAGE
    assert "no action 'lint[typo]'" in h.err.getvalue()
    assert "lint" in h.err.getvalue()  # lists what the plan has
    assert h.store.results == {}
    assert h.scratch_entries() == []  # nothing staged


# R1
def test_selects_action_by_id(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    a = shell_spec("exit 0", action_id="a")
    b = shell_spec("exit 3", action_id="b")
    code, build = h.run(a, b, action_id="b")
    assert code == EXIT_OK
    assert set(h.store.results) == {(build, "b")}
    manifest = h.result(build, "b")
    assert manifest is not None
    assert manifest.exit_code == 3
    assert manifest.action_key == b.key


# R1
def test_unknown_plan_exit_64(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    code = h.runner().run(RunRequest(hash_bytes(b"no such plan"), "s", None))
    assert code == EXIT_USAGE
    assert "plan" in h.err.getvalue()


# R1: the plan is read from the CAS by digest and must hash to it.
def test_corrupt_plan_exit_76(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    digest = store_plan(cas, shell_spec("true"))
    blob = cas.blob_path(digest)
    blob.chmod(0o644)
    blob.write_bytes(blob.read_bytes().replace(b'"true"', b'"echo"'))
    code = h.runner().run(RunRequest(digest, "s", None))
    assert code == EXIT_VERIFY
    assert "does not match" in h.err.getvalue()


# R1
def test_plan_of_other_domain_exit_64(tmp_path: Path) -> None:
    other = FsCAS(tmp_path / "cas", "other")
    h = Harness(tmp_path, other)
    code, _ = h.run(shell_spec("true"))  # the spec says domain "test"
    assert code == EXIT_USAGE
    assert "domain" in h.err.getvalue()


# R1
def test_unrefined_action_exit_64(cas: FsCAS, tmp_path: Path) -> None:
    import dataclasses

    from ebs.plan.types import ActionOutputInput, InputRef

    spec = shell_spec("true")
    pending = dataclasses.replace(
        spec, inputs=(InputRef("dep", "tree", ActionOutputInput("p", "out"), None),), key=None
    )
    h = Harness(tmp_path, cas)
    code, _ = h.run(pending)
    assert code == EXIT_USAGE
    assert "refine" in h.err.getvalue()


# R1: rule version on the node must match the planner's (else keys would lie).
def test_rule_version_mismatch_exit_64(cas: FsCAS, tmp_path: Path) -> None:
    import dataclasses

    from ebs.plan.keys import with_key
    from ebs.plan.types import RuleRef

    spec = with_key(dataclasses.replace(shell_spec("true"), rule=RuleRef("shell", "999")))
    h = Harness(tmp_path, cas)
    code, _ = h.run(spec)
    assert code == EXIT_USAGE
    assert "version" in h.err.getvalue()


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--plan", "sha256:" + "0" * 64],
        ["--plan", "md5:x", "--action", "a", "--domain", "test"],
        ["--plan", "sha256:" + "0" * 64, "--action", "a"],  # no --build and no --domain
        ["--plan", "sha256:" + "0" * 64, "--array-map", "sha256:" + "0" * 64, "--domain", "t"],
    ],
)
def test_bad_usage_exit_64(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[cas]\nroot = "{tmp_path / "cas"}"\n')
    monkeypatch.setenv("EBS_CONFIG", str(config))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no user config
    monkeypatch.chdir(tmp_path)
    assert main(argv) == EXIT_USAGE


def test_main_runs_action_without_build(
    cas: FsCAS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    digest = store_plan(cas, shell_spec("echo hello"))
    config = tmp_path / "config.toml"
    config.write_text(
        f'[cas]\nroot = "{tmp_path / "cas"}"\n[scratch]\ndir = "{tmp_path / "scratch"}"\n'
    )
    monkeypatch.setenv("EBS_CONFIG", str(config))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no user config
    monkeypatch.chdir(tmp_path)
    code = main(["--plan", str(digest), "--action", "s", "--domain", "test"])
    assert code == EXIT_OK
    manifest = json.loads(capsys.readouterr().out)  # no metadata: the result goes to stdout
    assert manifest["status"] == "passed"
    assert list((tmp_path / "scratch").iterdir()) == []


# R2: scratch that cannot be created (disk full, permissions) is temporary, not a runner bug.
def test_scratch_create_failure_is_infra(cas: FsCAS, tmp_path: Path) -> None:
    from ebs.runner.main import EXIT_INFRA

    blocker = tmp_path / "scratch-is-a-file"
    blocker.write_text("")
    h = Harness(tmp_path, cas, settings_overrides={"scratch_dir": blocker})
    code, _ = h.run(shell_spec("true"))
    assert code == EXIT_INFRA
    assert "scratch" in h.err.getvalue()


# R2: another runner of the same build may remove the empty build dir while this one creates
# its action dir (packed job arrays); creation retries instead of failing.
def test_scratch_create_survives_concurrent_build_dir_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ebs.runner import stage

    real_mkdir = Path.mkdir
    calls = {"n": 0}

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # the other runner's remove() wins once
            raise FileNotFoundError(2, "No such file or directory", str(self))
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    s = stage.Scratch.create(tmp_path, "7", "a")
    assert s.work.is_dir()
