"""`ebs build --ci` for GitLab pipelines (P2-07 R1, R3, R5)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ebs.ci.gitlab import (
    DOTENV_FILE,
    JUNIT_FILE,
    MASK,
    SUMMARY_FILE,
    CiDefaults,
    CiJob,
    CiSummary,
    Redactor,
    build_link,
    ci_defaults,
    require_token,
    write_summary,
)
from ebs.ci.junit import TestCase
from ebs.cli._records import BuildRecord, build_dir
from ebs.core.errors import ConfigError, ExitCode
from ebs.driver.events import read_events
from ebs.meta.api import BuildId
from ebs.sources.snapshot import SourceSnapshotter
from ebs.sources.statcache import StatCache
from tests.helpers.cli import Site, TestServices, write_site

TOKEN = "glpat-team-cpu-0123456789abcdef"
JOB_TOKEN = "job-token-fedcba9876543210"
GITLAB_ENV = {
    "GITLAB_CI": "true",
    "CI": "true",
    "CI_PROJECT_PATH": "cpu/verif",
    "CI_PIPELINE_ID": "4242",
    "CI_JOB_URL": "https://gitlab.example/cpu/verif/-/jobs/777",
    "CI_COMMIT_SHA": "0123456789abcdef0123456789abcdef01234567",
    "CI_JOB_TOKEN": JOB_TOKEN,
    "EBS_META_TOKEN": TOKEN,
}


class _CiServices(TestServices):
    """Also records how sources were snapshotted (`--rehash`)."""

    __test__ = False

    def __init__(self, root: Path, **kwargs: bool) -> None:
        super().__init__(root, **kwargs)
        self.rehash: list[bool] = []

    def snapshotter(self, cas: object, statcache: StatCache, *, rehash: bool) -> SourceSnapshotter:
        self.rehash.append(rehash)
        return super().snapshotter(cas, statcache, rehash=rehash)  # type: ignore[arg-type]


def _site(tmp_path: Path, *, tty: bool = False, config_extra: str = "") -> Site:
    write_site(tmp_path, config_extra=config_extra)
    services = _CiServices(tmp_path, tty=tty)
    services.environ.update(GITLAB_ENV, CI_PROJECT_DIR=str(tmp_path / "proj"))
    return Site(tmp_path, services)


def _records(site: Site) -> list[BuildRecord]:
    found = [BuildRecord.read(d) for d in (site.proj / ".ebs" / "builds").iterdir()]
    return sorted(found, key=lambda r: int(r.build))


def _record(site: Site) -> BuildRecord:
    (only,) = _records(site)
    return only


# R1: --ci means cache write, rehash, no live display, CI QOS.
def test_ci_defaults(tmp_path: Path) -> None:
    assert ci_defaults(None, no_cache=False) == CiDefaults(
        cache_mode="write", rehash=True, interactive=False, build_kind="ci"
    )
    assert ci_defaults("write", no_cache=False).cache_mode == "write"
    for cache, no_cache in [("read", False), ("off", False), (None, True)]:
        with pytest.raises(ConfigError, match="--ci"):
            ci_defaults(cache, no_cache=no_cache)

    site = _site(tmp_path, tty=True)  # even on a terminal: one line per event, no Live table
    result = site.invoke(["build", "--ci"])
    assert result.exit_code == ExitCode.OK, result.output
    services = site.services
    assert isinstance(services, _CiServices)
    assert services.rehash == [True]
    assert services.executor_args == [{"project": "demo", "build_kind": "ci"}]
    build = services.memory_store.get_build(_record(site).build)
    assert build.cache_mode == "write"
    assert "\x1b" not in result.stdout
    assert any(line.startswith("finished ") for line in result.stdout.splitlines())

    # without --ci: read cache, stat cache trusted, dev QOS
    services.rehash.clear()
    services.executor_args.clear()
    assert site.invoke(["build"]).exit_code == ExitCode.OK
    assert services.rehash == [False]
    assert services.executor_args == [{"project": "demo", "build_kind": "dev"}]

    conflict = site.invoke(["build", "--ci", "--cache", "read"])
    assert conflict.exit_code == ExitCode.USAGE
    assert "--ci" in conflict.output


# R1: the CI job's project, pipeline, job URL and commit are stored with the build.
def test_metadata_recorded(tmp_path: Path) -> None:
    site = _site(tmp_path)
    result = site.invoke(["build", "--ci"])
    assert result.exit_code == ExitCode.OK, result.output
    expected = {
        "project_path": "cpu/verif",
        "pipeline_id": "4242",
        "job_url": "https://gitlab.example/cpu/verif/-/jobs/777",
        "commit": "0123456789abcdef0123456789abcdef01234567",
    }
    record = _record(site)
    assert record.ci == expected
    build = site.services.memory_store.get_build(record.build)
    assert build.ci_job == expected["job_url"]
    events = read_events(build_dir(site.proj, record.uuid) / "events.jsonl")
    assert events[0].type == "build_started"
    assert events[0].data["ci"] == expected

    assert CiJob.from_environ({}).metadata() == {}  # --ci outside GitLab: nothing to record
    assert CiJob.from_environ({}).label is None
    partial = CiJob.from_environ({"CI_PROJECT_PATH": "a/b", "CI_PIPELINE_ID": "9"})
    assert partial.label == "a/b pipeline 9"
    long = CiJob.from_environ({"CI_JOB_URL": "https://gitlab.example/" + "x" * 400})
    assert long.label is not None
    assert len(long.label) <= 256

    # a plain build records no CI job
    assert site.invoke(["build"]).exit_code == ExitCode.OK
    plain = _records(site)[-1]
    assert plain.ci is None
    assert site.services.memory_store.get_build(plain.build).ci_job is None


# R3: ebs-summary.md with the build link and summary, ebs.env with EBS_BUILD_ID, JUnit report.
def test_summary_files(tmp_path: Path) -> None:
    site = _site(tmp_path, config_extra='[ci]\nbuild_url = "https://ebs.example/builds/{uuid}"\n')
    out_dir = tmp_path / "ci-project"
    out_dir.mkdir()
    site.services.environ["CI_PROJECT_DIR"] = str(out_dir)
    site.services.scripts = {"sim[test=random]": ("fail",)}
    result = site.invoke(["build", "--ci", "-k"])
    assert result.exit_code == ExitCode.ACTIONS_FAILED, result.output
    record = _record(site)

    dotenv = (out_dir / DOTENV_FILE).read_text()
    assert dotenv == f"EBS_BUILD_ID={int(record.build)}\n"
    summary = (out_dir / SUMMARY_FILE).read_text()
    assert f"https://ebs.example/builds/{record.uuid}" in summary
    assert f"build {int(record.build)}" in summary
    assert "failed" in summary
    assert "random" in summary  # the failed test is listed
    assert "| sim |" in summary  # per-step counts
    junit = ET.parse(out_dir / JUNIT_FILE).getroot()
    names = sorted(tc.get("name", "") for tc in junit.iter("testcase"))
    assert names == ["random", "smoke"]

    # no build_url configured: the summary still names the build
    assert build_link({}, BuildId(3), "u-1") is None
    assert build_link({"build_url": "https://x.example/b/{build}"}, BuildId(3), "u") == (
        "https://x.example/b/3"
    )
    with pytest.raises(ConfigError, match=r"\[ci\]\.build_url"):
        build_link({"build_url": "https://x.example/{nope}"}, BuildId(3), "u")


# R3: the summary writer on its own (failures listed, bounded).
def test_write_summary_lists_failures(tmp_path: Path) -> None:
    failures = [
        TestCase(classname="sim", name=f"t{i}[1]", state="failed", message=f"sig {i}")
        for i in range(30)
    ]
    files = write_summary(
        tmp_path,
        CiSummary(build=BuildId(5), uuid="u-5", project="demo", status="failed",
                  counts={"sim": {"done": 1, "failed": 30}}, link=None, failures=failures),
        redact=Redactor(()),
    )  # fmt: skip
    text = files.summary.read_text()
    assert "t0[1]" in text
    assert "sig 0" in text
    assert "t29[1]" not in text  # bounded list
    assert "and 10 more" in text
    assert "ebs status u-5" in text
    assert files.dotenv.read_text() == "EBS_BUILD_ID=5\n"


# R5: CI authenticates with the team's service-account token; the token never reaches the output,
# the summary, the JUnit report, the build record or the metadata store.
def test_token_redacted(tmp_path: Path) -> None:
    redact = Redactor.from_environ(GITLAB_ENV)
    assert redact(f"Authorization: Bearer {TOKEN} and {JOB_TOKEN}") == (
        f"Authorization: Bearer {MASK} and {MASK}"
    )
    assert Redactor.from_environ({}).__call__("plain") == "plain"
    assert repr(redact) == "Redactor(2 secrets)"
    assert TOKEN not in repr(redact)

    # the summary and the JUnit report go through it, whatever the log says
    leaky = TestCase(
        classname="sim",
        name="t[1]",
        state="failed",
        message=f"auth failed for {TOKEN}",
        system_out=f"env: EBS_META_TOKEN={TOKEN}",
    )
    files = write_summary(
        tmp_path / "w",
        CiSummary(build=BuildId(1), uuid="u", project="demo", status="failed",
                  counts={"sim": {"failed": 1}}, link=None, failures=[leaky]),
        redact=redact,
    )  # fmt: skip
    assert TOKEN not in files.summary.read_text()

    site = _site(tmp_path / "site")
    site.services.scripts = {"sim[test=random]": ("fail",)}
    result = site.invoke(["build", "--ci", "-k", "--json"])
    assert result.exit_code == ExitCode.ACTIONS_FAILED, result.output
    assert TOKEN not in result.output
    assert JOB_TOKEN not in result.output
    for path in (tmp_path / "site").rglob("*"):
        if path.is_file() and "cas" not in path.parts:
            text = path.read_bytes()
            assert TOKEN.encode() not in text, path
            assert JOB_TOKEN.encode() not in text, path
    store = site.services.memory_store
    stored = json.dumps(
        [store.get_build(r.build).model_dump(mode="json") for r in _records(site)]
        + [e.model_dump(mode="json") for e in store._events]
    )
    assert TOKEN not in stored
    assert JOB_TOKEN not in stored


# R5: --ci against the HTTP metadata service needs a bearer token (no MUNGE on CI runners).
def test_ci_requires_token(tmp_path: Path) -> None:
    http = {"url": "https://ebs-meta.example"}
    with pytest.raises(ConfigError, match="EBS_META_TOKEN") as err:
        require_token(http, {})
    assert "service account" in str(err.value)
    with pytest.raises(ConfigError, match="token"):
        require_token({**http, "auth": "munge"}, {"EBS_META_TOKEN": TOKEN})
    require_token(http, {"EBS_META_TOKEN": TOKEN})
    require_token({**http, "token_file": str(tmp_path / "t")}, {})
    require_token({"url": "postgresql+psycopg://ebs@db/ebs"}, {})  # direct database: dev only
    require_token({}, {})
