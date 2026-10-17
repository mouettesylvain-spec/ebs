"""Typed adapter over the SLURM client commands (P1-01).

`SlurmCli` wraps `sbatch --parsable`, `squeue --json`, `sacct --json`, `scancel` and
`scontrol --version`. Every command is an argv list built from typed options (never a shell
string), run through an injectable `CommandRunner`, and retried with exponential backoff when
SLURM reports a transient failure (controller or database unreachable, socket timeout). Any other
non-zero exit raises `ExecutorError` at once, quoting SLURM's message.

Retrying `sbatch` after a socket timeout can submit a job twice if the controller accepted the
first request before the reply was lost; the executor (P1-02) reconciles by job name.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Protocol, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from ebs.core.clock import Clock, SystemClock
from ebs.core.errors import ExecutorError
from ebs.core.log import get_logger
from ebs.exec.api import NodeName
from ebs.exec.slurm.models import (
    ArrayTaskId,
    JobId,
    JobRef,
    SacctJob,
    SacctResponse,
    SlurmJobInfo,
    SqueueJob,
    SqueueResponse,
    array_spec_contains,
    slurm_release,
)

__all__ = [
    "DEFAULT_COMMAND_TIMEOUT_S",
    "DEFAULT_MAX_RETRIES",
    "ArraySpec",
    "CommandResult",
    "CommandRunner",
    "SbatchOptions",
    "SlurmCli",
    "SubprocessRunner",
    "is_transient",
    "sbatch_argv",
    "write_env_file",
]

log = get_logger(__name__)

_Response = TypeVar("_Response", SqueueResponse, SacctResponse)

DEFAULT_MAX_RETRIES: Final = 5
DEFAULT_COMMAND_TIMEOUT_S: Final = 60.0
_BACKOFF_BASE_S: Final = 1.0
_BACKOFF_MAX_S: Final = 30.0

# Messages of failures that go away on their own: the controller or slurmdbd is busy, failing
# over or briefly unreachable. Matched case-insensitively anywhere in the output.
_TRANSIENT: Final = re.compile(
    "|".join(
        [
            r"socket timed out",
            r"timed out after",  # our own subprocess timeout
            r"unable to contact slurm controller",
            r"slurmctld not responding",
            r"slurm controller not responding",
            r"backup controller in standby mode",
            r"resource temporarily unavailable",
            r"temporarily unable to accept job",
            r"problem talking to the database",
            r"connection refused",
            r"connection reset by peer",
            r"zero bytes were transmitted or received",
            r"transaction aborted",
        ]
    ),
    re.IGNORECASE,
)
# scancel of a job that already ended: not an error for Executor.cancel.
_CANCEL_HARMLESS: Final = re.compile(
    r"already completing or completed|invalid job id specified", re.IGNORECASE
)
_INVALID_JOB_ID: Final = re.compile(r"invalid job id specified", re.IGNORECASE)
_ENV_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# --- running commands ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        """Run argv (a list, no shell) and capture its output."""
        ...


class SubprocessRunner:
    """Runs SLURM commands as subprocesses with the caller's environment (SLURM_CONF etc.)."""

    def __call__(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        try:
            done = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"{argv[0]}: timed out after {timeout_s:g} s")
        except FileNotFoundError as exc:
            raise ExecutorError(
                f"SLURM command {argv[0]!r} not found: run ebs on a SLURM login node, put the "
                "SLURM client on PATH, or set [slurm].bin_dir in ebs.toml"
            ) from exc
        return CommandResult(done.returncode, done.stdout, done.stderr)


def is_transient(message: str) -> bool:
    """Whether a SLURM client error is worth retrying."""
    return _TRANSIENT.search(message) is not None


# --- sbatch options --------------------------------------------------------------------------

_Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.+-]+$")]
_Text = Annotated[str, StringConstraints(min_length=1)]
_License = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.+-]+(@[A-Za-z0-9_.+-]+)?$")]


class ArraySpec(BaseModel):
    """`--array=<indices>%<throttle>`: which task ids to run and how many at once."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    indices: tuple[Annotated[int, Field(ge=0)], ...] = Field(min_length=1)
    throttle: Annotated[int, Field(ge=1)] | None = None

    @field_validator("indices")
    @classmethod
    def _unique(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(set(value)) != len(value):
            raise ValueError("array indices must be unique")
        return value

    def render(self) -> str:
        parts: list[str] = []
        ordered = sorted(self.indices)
        start = prev = ordered[0]
        for i in [*ordered[1:], None]:
            if i is not None and i == prev + 1:
                prev = i
                continue
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            if i is not None:
                start = prev = i
        text = ",".join(parts)
        return f"{text}%{self.throttle}" if self.throttle is not None else text


class SbatchOptions(BaseModel):
    """Everything ebs passes to `sbatch`. The environment travels in `export_file` only."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    job_name: _Text
    partition: _Name | None = None
    account: _Name | None = None
    qos: _Name | None = None
    cpus_per_task: Annotated[int, Field(ge=1)] | None = None
    mem_mb: Annotated[int, Field(ge=1)] | None = None
    time_limit_s: Annotated[int, Field(ge=1)] | None = None
    licenses: Mapping[_License, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    constraint: _Text | None = None
    exclude: tuple[NodeName, ...] = ()  # P1-05 R3: nodes that failed this build
    array: ArraySpec | None = None
    output: Path | None = None  # may hold sbatch patterns: %j %A %a %x
    error: Path | None = None
    workdir: Path | None = None
    export_file: Path | None = None  # NUL-separated NAME=value (write_env_file)
    requeue: bool = False  # the driver retries; SLURM requeues would run an action twice
    comment: _Text | None = None  # P1-02: the build UUID, for admin tooling and the epilog

    @field_validator("job_name", "constraint", "comment")
    @classmethod
    def _printable(cls, value: str | None) -> str | None:
        if value is not None and any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("must not contain control characters")
        if value is not None and value != value.strip():
            raise ValueError("must not start or end with whitespace")
        return value

    @field_validator("constraint")
    @classmethod
    def _no_space(cls, value: str | None) -> str | None:
        if value is not None and any(c.isspace() for c in value):
            raise ValueError("a constraint expression has no whitespace")
        return value


def _duration(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{days}-{hours:02d}:{minutes:02d}:{secs:02d}"


def sbatch_argv(script: Path, opts: SbatchOptions, *, command: str = "sbatch") -> list[str]:
    """The sbatch argv for `opts` (golden-tested; P1-01 R1).

    Each option is one `--name=value` element, so a value starting with `-` can't be read as
    another option. The job never inherits the submitter's environment (`--export=NONE`).
    """
    argv = [command, "--parsable", f"--job-name={opts.job_name}"]
    if opts.partition is not None:
        argv.append(f"--partition={opts.partition}")
    if opts.account is not None:
        argv.append(f"--account={opts.account}")
    if opts.qos is not None:
        argv.append(f"--qos={opts.qos}")
    if opts.cpus_per_task is not None:
        argv.append(f"--cpus-per-task={opts.cpus_per_task}")
    if opts.mem_mb is not None:
        argv.append(f"--mem={opts.mem_mb}M")
    if opts.time_limit_s is not None:
        argv.append(f"--time={_duration(opts.time_limit_s)}")
    if opts.licenses:
        lic = ",".join(f"{name}:{count}" for name, count in sorted(opts.licenses.items()))
        argv.append(f"--licenses={lic}")
    if opts.constraint is not None:
        argv.append(f"--constraint={opts.constraint}")
    if opts.exclude:
        argv.append(f"--exclude={','.join(sorted(opts.exclude))}")
    if opts.array is not None:
        argv.append(f"--array={opts.array.render()}")
    if opts.output is not None:
        argv.append(f"--output={opts.output}")
    if opts.error is not None:
        argv.append(f"--error={opts.error}")
    if opts.workdir is not None:
        argv.append(f"--chdir={opts.workdir}")
    if opts.comment is not None:
        argv.append(f"--comment={opts.comment}")
    argv.append("--export=NONE")
    if opts.export_file is not None:
        argv.append(f"--export-file={opts.export_file}")
    argv.append("--requeue" if opts.requeue else "--no-requeue")
    argv.append(str(script.absolute()))
    return argv


def write_env_file(path: Path, env: Mapping[str, str]) -> None:
    """Write `env` for `sbatch --export-file`: NAME=value entries, each ended by a NUL byte."""
    chunks: list[bytes] = []
    for name in sorted(env):
        value = env[name]
        if not _ENV_NAME.match(name):
            raise ExecutorError(f"invalid environment variable name {name!r} for the job")
        if "\x00" in value:
            raise ExecutorError(f"environment variable {name} contains a NUL byte")
        chunks.append(f"{name}={value}\x00".encode())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"".join(chunks))


# --- the adapter -----------------------------------------------------------------------------


def _job_sort_key(job: str) -> tuple[int, str]:
    return (int(job), job) if job.isdigit() else (1 << 62, job)


def _ref_text(ref: JobRef) -> str:
    return str(ref)


def _base_job(ref: JobRef) -> JobId:
    return ref.array_job_id if isinstance(ref, ArrayTaskId) else ref


class _Records:
    """squeue/sacct records indexed by job id and array task: lookups cost O(1), not O(records),
    so one poll of thousands of array tasks stays linear (P1-02 R5).

    When several records answer a reference (a requeued task, or a task that started while its
    array's pending record was read), the later record wins, in the order SLURM listed them.
    """

    def __init__(self, jobs: Sequence[SqueueJob | SacctJob]) -> None:
        self._jobs = jobs
        self._infos = [job.info() for job in jobs]
        self._by_id: dict[JobId, int] = {}
        self._by_task: dict[tuple[JobId, int], int] = {}
        self._pending: dict[JobId, list[tuple[int, str]]] = {}  # array -> (record, "2-9%2")
        for i, (job, info) in enumerate(zip(jobs, self._infos, strict=True)):
            self._by_id[info.job_id] = i
            if info.array_job_id is None:
                continue
            if info.array_task_id is not None:
                self._by_task[(info.array_job_id, info.array_task_id)] = i
                continue
            spec = job.array_task_string if isinstance(job, SqueueJob) else job.array.task
            if spec:
                self._pending.setdefault(info.array_job_id, []).append((i, spec))

    def match(self, ref: JobRef, out: dict[JobRef, SlurmJobInfo]) -> bool:
        """Put what the records say about `ref` into `out`; False if they do not know it."""
        if not isinstance(ref, ArrayTaskId):
            i = self._by_id.get(ref)
            if i is None:
                return False
            out[ref] = self._infos[i]
            return True
        own = self._by_task.get((ref.array_job_id, ref.task_id), -1)
        collapsed = max(
            (
                i
                for i, spec in self._pending.get(ref.array_job_id, ())
                if array_spec_contains(spec, ref.task_id)
            ),
            default=-1,
        )
        if own < 0 and collapsed < 0:
            return False
        out[ref] = (
            self._infos[own] if own > collapsed else self._jobs[collapsed].info(task_id=ref.task_id)
        )
        return True


class SlurmCli:
    """The SLURM client commands, typed. One instance per executor; holds no job state."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        clock: Clock | None = None,
        bin_dir: Path | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._clock = clock or SystemClock()
        self._bin_dir = bin_dir
        self._max_retries = max_retries
        self._timeout_s = timeout_s

    def _cmd(self, name: str) -> str:
        return str(self._bin_dir / name) if self._bin_dir is not None else name

    def _run(self, argv: list[str], *, ok_if: re.Pattern[str] | None = None) -> CommandResult:
        name = Path(argv[0]).name
        attempts = self._max_retries + 1
        delay = _BACKOFF_BASE_S
        for attempt in range(1, attempts + 1):
            result = self._runner(argv, timeout_s=self._timeout_s)
            if result.returncode == 0:
                return result
            message = (result.stderr.strip() or result.stdout.strip()) or (
                f"exit status {result.returncode}"
            )
            if ok_if is not None:
                # Lines matching ok_if are expected (e.g. "already completed"); the rest
                # decides between done, retry and error.
                rest = [ln for ln in message.splitlines() if ln.strip() and not ok_if.search(ln)]
                if not rest:
                    return result
                message = "; ".join(rest)
            if not is_transient(message):
                raise ExecutorError(f"{name} failed: {message}")
            if attempt == attempts:
                raise ExecutorError(
                    f"{name} still failing after {attempts} attempts (SLURM controller or "
                    f"database unreachable?): {message}"
                )
            log.warning(
                "transient SLURM failure, retrying",
                command=name,
                attempt=attempt,
                delay_s=delay,
                message=message,
            )
            self._clock.sleep(delay)
            delay = min(delay * 2, _BACKOFF_MAX_S)
        raise AssertionError("unreachable")  # pragma: no cover

    # sbatch ------------------------------------------------------------------------------

    def submit(self, script: Path, opts: SbatchOptions) -> JobId:
        """Submit `script` with `sbatch --parsable`; returns the (array) job id."""
        result = self._run(sbatch_argv(script, opts, command=self._cmd("sbatch")))
        first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        job = first.split(";", 1)[0].strip()
        if not job.isdigit():
            raise ExecutorError(
                f"sbatch --parsable printed {result.stdout.strip()!r}, expected a job id; "
                "is the SLURM client wrapped by a site script?"
            )
        log.info("submitted", job_id=job, job_name=opts.job_name)
        return JobId(job)

    # squeue / sacct ----------------------------------------------------------------------

    def query(self, job_ids: Iterable[JobRef]) -> dict[JobRef, SlurmJobInfo]:
        """What SLURM knows about each job: one squeue call, then one sacct call for the rest.

        squeue runs with `--states=all`, so jobs that ended within slurmctld's MinJobAge are
        answered without touching slurmdbd. Jobs unknown to both are absent from the result.
        """
        refs = list(dict.fromkeys(job_ids))
        if not refs:
            return {}
        result: dict[JobRef, SlurmJobInfo] = {}
        live = _Records(self._squeue(sorted({_base_job(r) for r in refs}, key=_job_sort_key)))
        missing = [r for r in refs if not live.match(r, result)]
        if missing:
            done = _Records(self._sacct(sorted({_base_job(r) for r in missing}, key=_job_sort_key)))
            for ref in missing:
                done.match(ref, result)
        return result

    def _squeue(self, jobs: list[JobId]) -> list[SqueueJob]:
        argv = [self._cmd("squeue"), "--json", "--states=all", f"--jobs={','.join(jobs)}"]
        result = self._run(argv, ok_if=_INVALID_JOB_ID)
        if result.returncode != 0:  # every job id unknown to slurmctld
            return []
        return self._parse(result.stdout, SqueueResponse, "squeue").jobs

    def _sacct(self, jobs: list[JobId]) -> list[SacctJob]:
        argv = [self._cmd("sacct"), "--json", f"--jobs={','.join(jobs)}"]
        return self._parse(self._run(argv).stdout, SacctResponse, "sacct").jobs

    @staticmethod
    def _parse(text: str, model: type[_Response], command: str) -> _Response:
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ExecutorError(
                f"{command} --json printed something that is not JSON ({exc}); "
                f"first bytes: {text[:200]!r}"
            ) from exc
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            meta = raw.get("meta") if isinstance(raw, dict) else None
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
            )
            raise ExecutorError(
                f"{command} --json output from SLURM {slurm_release(meta)} lacks fields ebs "
                f"needs ({problems}); ebs supports the SLURM 26.05 JSON format, please report "
                "this with the output of the command"
            ) from exc

    # scancel / scontrol ------------------------------------------------------------------

    def cancel(self, job_ids: Iterable[JobRef], *, signal: str | None = None) -> None:
        """scancel the jobs in one call; jobs that already ended are not an error."""
        refs = sorted(
            {_ref_text(r) for r in job_ids}, key=lambda t: (*_job_sort_key(t.split("_")[0]), t)
        )
        if not refs:
            return
        argv = [self._cmd("scancel")]
        if signal is not None:
            argv.append(f"--signal={signal}")
        self._run([*argv, *refs], ok_if=_CANCEL_HARMLESS)

    def version(self) -> str:
        """The SLURM release of the client, e.g. "26.05.0"."""
        out = self._run([self._cmd("scontrol"), "--version"]).stdout.strip()
        return out.split()[-1] if out else "unknown"
