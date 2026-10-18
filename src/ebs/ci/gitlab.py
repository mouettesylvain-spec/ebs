"""`ebs build --ci` in a GitLab pipeline (P2-07 R1, R3, R5).

- `ci_defaults`: `--ci` means `--cache=write`, `--rehash`, no live display and the CI QOS
  (`[slurm.qos].ci`, P1-03); an explicit other `--cache` is a usage error.
- `CiJob`: the job metadata stored with the build (the `build_started` event, `.ebs/builds/…`,
  and the build's `ci_job` column): `CI_PROJECT_PATH`, `CI_PIPELINE_ID`, `CI_JOB_URL`,
  `CI_COMMIT_SHA`. Only these variables are read, never the job's tokens.
- `write_summary`: `$CI_PROJECT_DIR/ebs-summary.md` (link, per-step counts, failed tests) and the
  dotenv artifact `ebs.env` (`EBS_BUILD_ID=<id>`) for later jobs (`ebs channel set --if-passing`).
- `require_token` / `Redactor`: CI talks to the metadata service with the team's service-account
  bearer token (`$EBS_META_TOKEN`, a masked CI variable, or `[metadata].token_file`), and every
  text ebs writes in CI mode (summary, JUnit report) has known secrets masked.
"""

from __future__ import annotations

import os
import string
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ebs.ci.junit import TestCase
from ebs.core.errors import ConfigError
from ebs.exec.slurm.accounts import BuildKind
from ebs.meta.api import BuildId, CacheMode

__all__ = [
    "CI_METADATA",
    "DOTENV_FILE",
    "JUNIT_FILE",
    "MASK",
    "SECRET_ENV",
    "SUMMARY_FILE",
    "CiDefaults",
    "CiJob",
    "CiSummary",
    "Redactor",
    "SummaryFiles",
    "build_link",
    "ci_defaults",
    "output_dir",
    "require_token",
    "write_summary",
]

SUMMARY_FILE: Final = "ebs-summary.md"
DOTENV_FILE: Final = "ebs.env"
JUNIT_FILE: Final = "ebs-junit.xml"
MASK: Final = "[MASKED]"

CI_METADATA: Final = {
    "project_path": "CI_PROJECT_PATH",
    "pipeline_id": "CI_PIPELINE_ID",
    "job_url": "CI_JOB_URL",
    "commit": "CI_COMMIT_SHA",
}
"""Stored key -> GitLab predefined variable."""

SECRET_ENV: Final = (
    "EBS_META_TOKEN",
    "CI_JOB_TOKEN",
    "CI_REGISTRY_PASSWORD",
    "CI_DEPLOY_PASSWORD",
    "CI_JOB_JWT",
    "CI_JOB_JWT_V2",
)
"""Variables whose values are masked in everything ebs writes in CI mode."""

_LABEL_MAX: Final = 256  # the build's `ci_job` column (meta.api `_Name`)
_MAX_LISTED: Final = 20  # failed tests listed in the summary
_MIN_SECRET: Final = 4  # shorter values would mask ordinary text


@dataclass(frozen=True)
class CiDefaults:
    cache_mode: CacheMode
    rehash: bool
    interactive: bool
    build_kind: BuildKind


def ci_defaults(cache: str | None, *, no_cache: bool) -> CiDefaults:
    """What `--ci` implies; ConfigError if `--cache`/`--no-cache` contradict it."""
    if no_cache or cache not in (None, "write"):
        given = "--no-cache" if no_cache else f"--cache {cache}"
        raise ConfigError(
            f"--ci conflicts with {given}: CI builds write the shared action cache (--cache "
            "write); drop the option, or drop --ci for a private build"
        )
    return CiDefaults(cache_mode="write", rehash=True, interactive=False, build_kind="ci")


@dataclass(frozen=True)
class CiJob:
    """The GitLab job running this build (empty outside GitLab)."""

    values: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> CiJob:
        return cls({k: environ[v] for k, v in CI_METADATA.items() if environ.get(v)})

    def metadata(self) -> dict[str, str]:
        return dict(self.values)

    @property
    def label(self) -> str | None:
        """The build's `ci_job`: the job URL, else project and pipeline (bounded)."""
        v = self.values
        if "job_url" in v:
            text = v["job_url"]
        elif "project_path" in v or "pipeline_id" in v:
            text = " ".join(
                p for p in (v.get("project_path"), _pipeline(v.get("pipeline_id"))) if p
            )
        else:
            return None
        return text[:_LABEL_MAX]


def _pipeline(pipeline: str | None) -> str | None:
    return None if pipeline is None else f"pipeline {pipeline}"


class Redactor:
    """Masks secret values (CI tokens) in text."""

    def __init__(self, secrets: Iterable[str]) -> None:
        # Longest first, so a secret containing another is masked whole.
        found = {s for s in secrets if len(s) >= _MIN_SECRET}
        self._secrets = sorted(found, key=len, reverse=True)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str], extra: Iterable[str] = ()) -> Redactor:
        return cls([*(environ.get(name, "").strip() for name in SECRET_ENV), *extra])

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return text

    def __repr__(self) -> str:
        return f"Redactor({len(self._secrets)} secrets)"


def require_token(metadata: Mapping[str, object], environ: Mapping[str, str]) -> None:
    """ConfigError unless CI reaches an HTTP metadata service with a bearer token (R5)."""
    url = metadata.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return  # no store, or direct database access (development sites)
    if metadata.get("auth") == "munge":
        raise ConfigError(
            "--ci authenticates with a bearer token, but [metadata].auth = 'munge'; set auth = "
            "'token' (or remove it) in the CI runner's ebs.toml"
        )
    if metadata.get("token_file") or environ.get("EBS_META_TOKEN", "").strip():
        return
    raise ConfigError(
        "--ci needs the team's service account token for the metadata service: add it to the "
        "GitLab project as a masked, protected CI variable EBS_META_TOKEN (or set "
        "[metadata].token_file)"
    )


def build_link(section: Mapping[str, object], build: BuildId, uuid: str) -> str | None:
    """`[ci].build_url` with `{build}` and `{uuid}` filled in; None if unset."""
    template = section.get("build_url")
    if template is None:
        return None
    if not isinstance(template, str) or not template:
        raise ConfigError(f"[ci].build_url must be a URL template string, got {template!r}")
    fields = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    unknown = sorted(fields - {"build", "uuid"})
    if unknown:
        raise ConfigError(
            f"[ci].build_url {template!r}: unknown field(s) {', '.join(unknown)}; use {{build}} "
            "or {uuid}"
        )
    return template.format(build=int(build), uuid=uuid)


def output_dir(environ: Mapping[str, str], cwd: Path) -> Path:
    """Where CI files go: `$CI_PROJECT_DIR` (GitLab collects artifacts there), else `cwd`."""
    found = environ.get("CI_PROJECT_DIR")
    return Path(found) if found else cwd


@dataclass(frozen=True)
class CiSummary:
    build: BuildId
    uuid: str
    project: str
    status: str
    counts: Mapping[str, Mapping[str, int]]  # step -> final state -> actions
    link: str | None
    failures: Sequence[TestCase]


@dataclass(frozen=True)
class SummaryFiles:
    summary: Path
    dotenv: Path


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def _markdown(s: CiSummary) -> str:
    where = f"[build {int(s.build)}]({s.link})" if s.link else f"build {int(s.build)}"
    lines = [
        f"## ebs: {_cell(s.project)} {where}: {s.status}",
        "",
        f"`ebs status {s.uuid}` shows the build, `ebs logs` its actions.",
        "",
    ]
    states = sorted({state for per in s.counts.values() for state in per})
    if states:
        lines += [
            "| step | " + " | ".join(states) + " |",
            "|---|" + "---|" * len(states),
            *(
                f"| {_cell(step)} | " + " | ".join(str(per.get(x, 0)) for x in states) + " |"
                for step, per in s.counts.items()
            ),
            "",
        ]
    if s.failures:
        lines += [f"### Failed tests ({len(s.failures)})", ""]
        for case in s.failures[:_MAX_LISTED]:
            lines.append(f"- `{_cell(case.classname)}` {_cell(case.name)}: "
                         f"{_cell(case.message or '')}")  # fmt: skip
        if len(s.failures) > _MAX_LISTED:
            lines.append(f"- … and {len(s.failures) - _MAX_LISTED} more (see the JUnit report)")
        lines.append("")
    return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_summary(
    directory: Path, summary: CiSummary, *, redact: Callable[[str], str]
) -> SummaryFiles:
    """Write `ebs-summary.md` and `ebs.env` into `directory` (created if needed)."""
    directory.mkdir(parents=True, exist_ok=True)
    files = SummaryFiles(directory / SUMMARY_FILE, directory / DOTENV_FILE)
    _write(files.summary, redact(_markdown(summary)))
    _write(files.dotenv, f"EBS_BUILD_ID={int(summary.build)}\n")
    return files
