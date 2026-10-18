from __future__ import annotations

from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.core.errors import ConfigError
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


# R7 (P1-06): an http(s) [metadata].url selects the HTTP client; PostgreSQL stays for dev.
def test_selects_http_client() -> None:
    from ebs.meta.client import HttpMetadataStore
    from ebs.meta.pg import PgMetadataStore
    from ebs.runner.main import _open_store

    for url in ("https://meta.example.invalid", "http://meta.example.invalid:8080"):
        store = _open_store({"url": url, "auth": "munge"})
        assert isinstance(store, HttpMetadataStore)
        assert store.base_url == url
        store.close()
    pg = _open_store({"url": "postgresql+psycopg://ebs@db.example.invalid/ebs"})
    assert isinstance(pg, PgMetadataStore)
    pg.close()
    with pytest.raises(ConfigError, match=r"\[metadata\]\.url"):
        _open_store({"url": "ftp://meta.example.invalid"})


# R5 (P1-04): the runner says once that the tool runs, and where.
def test_running_event_once(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas, caller_env={"SLURM_JOB_ID": "4242"})
    code, build = h.run(shell_spec("echo a; echo b; exit 0"))
    assert code == EXIT_OK
    running = [e for e in h.store.events if e.type == "running"]
    assert len(running) == 1
    (event,) = running
    assert event.build == build
    assert event.action_id == "s"
    assert event.data == {"host": "node-test", "job_id": "4242"}


# R5: outside SLURM there is no job id.
def test_running_event_without_slurm(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    code, _ = h.run(shell_spec("true"))
    assert code == EXIT_OK
    (event,) = [e for e in h.store.events if e.type == "running"]
    assert event.data == {"host": "node-test", "job_id": None}


# R5: the lease is renewed at start and then every interval while the tool runs.
def test_heartbeat_renews_lease() -> None:
    from ebs.core.clock import FakeClock
    from ebs.runner.heartbeat import Heartbeat

    clock = FakeClock()
    renewals: list[float] = []
    beat = Heartbeat(clock, interval_s=60, renew=lambda: renewals.append(clock.monotonic()))
    beat.tick()
    for _ in range(12):
        clock.advance(10)
        beat.tick()
    assert renewals == [0, 60, 120]


# R5: a renewal that fails is logged, not raised: the tool keeps running.
def test_heartbeat_survives_renew_error() -> None:
    from ebs.core.clock import FakeClock
    from ebs.core.errors import MetadataError
    from ebs.runner.heartbeat import Heartbeat

    calls = {"n": 0}

    def renew() -> None:
        calls["n"] += 1
        raise MetadataError("service down")

    clock = FakeClock()
    beat = Heartbeat(clock, interval_s=1, renew=renew)
    beat.tick()
    clock.advance(1)
    beat.tick()
    assert calls["n"] == 2


# R5: the runner calls the renew hook while the tool runs.
def test_runner_renews_lease_while_running(cas: FsCAS, tmp_path: Path) -> None:
    renewals: list[int] = []
    h = Harness(tmp_path, cas, renew_lease=lambda: renewals.append(1))
    code, _ = h.run(shell_spec("true"))
    assert code == EXIT_OK
    assert renewals  # at least the initial renewal


class _SteppingClock:
    """A clock whose monotonic time advances 1 s per reading (heartbeats due at every poll)."""

    def __init__(self) -> None:
        from ebs.core.clock import FakeClock

        self._fake = FakeClock()

    def now(self):  # type: ignore[no-untyped-def]
        return self._fake.now()

    def monotonic(self) -> float:
        self._fake.advance(1)
        return self._fake.monotonic()

    def sleep(self, seconds: float) -> None:
        self._fake.advance(seconds)


# R5 / R3: the lease is renewed and the live log flushed while the tool runs, not only at start.
def test_renews_and_flushes_while_running(cas: FsCAS, tmp_path: Path) -> None:
    import dataclasses

    from ebs.plan.types import DebugSpec
    from ebs.runner.collect import debug_action_dir

    (tmp_path / "debug" / "test").mkdir(parents=True)
    renewals: list[int] = []
    h = Harness(
        tmp_path,
        cas,
        renew_lease=lambda: renewals.append(1),
        settings_overrides={"heartbeat_s": 1, "debug_root": tmp_path / "debug"},
    )
    runner = h.runner()
    runner._clock = _SteppingClock()
    live = debug_action_dir(tmp_path / "debug", "test", "1", "s") / "live.log"  # first build
    # The tool waits until its own early output shows up in the live log and exits 1 if it
    # does: it only gets there through flushes during the run.
    script = (
        "echo start\n"
        f"for i in $(seq 1 100); do grep -q start {live} && exit 1; sleep 0.05; done; exit 0\n"
    )
    spec = dataclasses.replace(shell_spec(script), debug=DebugSpec((), None, True))
    from tests.helpers.runner import new_build, store_plan

    plan = store_plan(cas, spec)
    build = new_build(h.store, "s", cache_mode="write")
    assert build == 1
    from ebs.runner.main import RunRequest

    assert runner.run(RunRequest(plan, "s", build)) == EXIT_OK, h.err.getvalue()
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.exit_code == 1  # saw its output in the live log while running
    assert len(renewals) >= 2


# R5 (with P1-09): without an injected hook the runner leases the inputs it staged.
def test_runner_leases_its_inputs(cas: FsCAS, tmp_path: Path) -> None:
    from tests.helpers.runner import source_file

    h = Harness(tmp_path, cas)
    src = source_file(cas, "in.txt", b"input")
    code, _ = h.run(shell_spec("true", inputs=[src]))
    assert code == EXIT_OK
    roots = h.store.gc_roots("test", h.store._clock.now())
    assert src.id in roots.leased


# R5: an expired lease is acquired again at the next beat.
def test_runner_lease_reacquired(cas: FsCAS, tmp_path: Path) -> None:
    import dataclasses

    from ebs.core.errors import MetadataError
    from ebs.runner.main import Runner, RunRequest
    from tests.helpers.runner import new_build, source_file, store_plan

    h = Harness(tmp_path, cas)
    calls: list[str] = []
    real_acquire, real_renew = h.store.acquire_lease, h.store.renew_lease

    def acquire(*a: object, **k: object) -> object:
        calls.append("acquire")
        return real_acquire(*a, **k)  # type: ignore[arg-type]

    def renew(*a: object, **k: object) -> None:
        calls.append("renew")
        raise MetadataError("lease expired")

    h.store.acquire_lease = acquire  # type: ignore[method-assign,assignment]
    h.store.renew_lease = renew  # type: ignore[method-assign]
    del real_renew
    src = source_file(cas, "in.txt", b"input")
    spec = shell_spec("true", inputs=[src])
    runner: Runner = h.runner()
    hook = runner._lease_hook(
        RunRequest(store_plan(cas, spec), "s", new_build(h.store, "s", cache_mode="read")),
        dataclasses.replace(spec),
        {"in.txt": src.id},  # type: ignore[dict-item]
    )
    assert hook is not None
    hook()
    hook()
    assert calls == ["acquire", "renew", "acquire"]


# R5: nothing to lease without a build (no store to post to).
def test_no_lease_without_build(cas: FsCAS, tmp_path: Path) -> None:
    from ebs.core.digest import hash_bytes
    from ebs.runner.main import RunRequest

    h = Harness(tmp_path, cas)
    spec = shell_spec("true")
    assert (
        h.runner()._lease_hook(
            RunRequest(hash_bytes(b"p"), "s", None), spec, {"a": hash_bytes(b"a")}
        )
        is None
    )


def _array_site(cas: FsCAS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[cas]\nroot = "{tmp_path / "cas"}"\n[scratch]\ndir = "{tmp_path / "scratch"}"\n'
    )
    monkeypatch.setenv("EBS_CONFIG", str(config))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no user config
    monkeypatch.chdir(tmp_path)


# P1-04 (array tasks): task $SLURM_ARRAY_TASK_ID runs actions[i] of the executor's array map.
def test_array_map_runs_the_task_action(
    cas: FsCAS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from ebs.exec.slurm.arrays import store_array_map

    plan = store_plan(cas, shell_spec("echo a", action_id="a"), shell_spec("exit 1", action_id="b"))
    array_map = store_array_map(cas, plan, ["a", "b"])
    _array_site(cas, tmp_path, monkeypatch)
    monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "1")
    code = main(["--plan", str(plan), "--array-map", str(array_map), "--domain", "test"])
    assert code == EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "failed"  # b ran, not a


@pytest.mark.parametrize("case", ["other_plan", "out_of_range", "no_task_id", "with_action"])
def test_array_map_mismatch_exit_64(
    case: str, cas: FsCAS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ebs.exec.slurm.arrays import store_array_map

    plan = store_plan(cas, shell_spec("echo a", action_id="a"), shell_spec("echo b", action_id="b"))
    other = store_plan(cas, shell_spec("echo c", action_id="a"))
    array_map = store_array_map(cas, other if case == "other_plan" else plan, ["a", "b"])
    _array_site(cas, tmp_path, monkeypatch)
    if case != "no_task_id":
        monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "2" if case == "out_of_range" else "0")
    else:
        monkeypatch.delenv("SLURM_ARRAY_TASK_ID", raising=False)
    argv = ["--plan", str(plan), "--array-map", str(array_map), "--domain", "test"]
    if case == "with_action":
        argv += ["--action", "a"]
    assert main(argv) == EXIT_USAGE
