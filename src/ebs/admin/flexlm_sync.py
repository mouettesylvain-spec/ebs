"""`ebs-flexlm-sync`: push FlexLM license usage into SLURM `LastConsumed` (P3-08).

architecture.md § Licenses: licenses are SLURM *remote* licenses (`sacctmgr add resource`).
SLURM only counts what its own jobs hold, so an interactive GUI session that checks out a
license outside SLURM would let a job start and then block on FlexLM. Every minute (systemd
timer, deploy/flexlm) this tool runs `lmutil lmstat -a -c <license>` for each configured server,
takes the licenses in use (by anyone) per configured feature, and sets
`sacctmgr -i update resource <feature> server=<server> set LastConsumed=<n>` where SLURM's
current value differs. SLURM then reserves `LastConsumed - (held by its jobs)` for outside use.

Failure policy: never guess. A server that is down, a vendor daemon that is down, a truncated
or failed `lmstat`, or an unreachable slurmdbd -> no update for that server and an error event
(`flexlm_server_unavailable` / `flexlm_slurm_unavailable`), exit 1 so the unit fails and the
site's OnFailure alerting fires. A configured feature missing from good output is left alone
(`flexlm_feature_unreported`). A feature that SLURM has no resource for is a configuration
mismatch: one warning (`flexlm_feature_not_in_slurm`) until the resource appears, remembered
across runs in `[flexlm].state_file`.

It runs as the admin account of deploy/flexlm (a SLURM operator) and refuses to run inside a
SLURM job, so a user build can never move the counters.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, TextIO

from ebs.config import config_paths, load_config
from ebs.core.errors import ConfigError, EbsError
from ebs.core.log import configure_logging, get_logger
from ebs.exec.slurm.licenses import FEATURE_PATTERN

__all__ = [
    "CommandResult",
    "CommandRunner",
    "FeatureUsage",
    "FlexlmError",
    "FlexlmServer",
    "FlexlmSync",
    "FlexlmSyncConfig",
    "SubprocessRunner",
    "SyncReport",
    "main",
    "parse_lmstat",
    "parse_resources",
]

log = get_logger(__name__)

EXIT_OK: Final = 0
EXIT_ALERT: Final = 1  # a server, a feature or an update needs attention
EXIT_USAGE: Final = 2

_NAME_RE: Final = re.compile(FEATURE_PATTERN, re.ASCII)
_FEATURE_SECTION: Final = re.compile(r"^Feature usage info:", re.MULTILINE)
_USERS_OF: Final = re.compile(r"^Users of (?P<name>[^:\s]+):\s+\((?P<body>.*)\)\s*$")
_COUNTS: Final = re.compile(
    r"Total of (?P<issued>\d+) licenses? issued;\s*Total of (?P<in_use>\d+) licenses? in use"
)
# lmstat often exits 0 while saying it could not reach the server or a vendor daemon.
# Anchored to lmstat's own phrasing, so a host name such as "lic-down" never matches.
_DOWN: Final = re.compile(
    r"Cannot connect to license server|lmgrd is not running|vendor daemon is down"
    r"|license server machine is down|^\S+: license server (?:down|not responding)"
    r"|Error getting status",
    re.IGNORECASE | re.MULTILINE,
)


class FlexlmError(EbsError):
    """`lmstat` output that cannot be trusted (server or vendor daemon down, truncated)."""


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
    """Runs `lmutil` and `sacctmgr`; a missing binary or a hang is a failed result, not a crash."""

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
        except FileNotFoundError:
            return CommandResult(127, "", f"{argv[0]}: command not found; set it in [flexlm]")
        return CommandResult(done.returncode, done.stdout, done.stderr)


# --- parsing ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureUsage:
    issued: int
    in_use: int


def parse_lmstat(text: str) -> dict[str, FeatureUsage]:
    """Counted features of `lmutil lmstat -a` output. Uncounted (node-locked) features and
    `Users of` lines with an error instead of counts are left out; a feature listed twice
    (two vendor daemons) is summed. Raises FlexlmError when the output says a server or vendor
    daemon is down, or has no feature section (truncated)."""
    down = _DOWN.search(text)
    if down is not None:
        line = text[text.rfind("\n", 0, down.start()) + 1 :].split("\n", 1)[0].strip()
        raise FlexlmError(f"license server unavailable: {line}")
    if _FEATURE_SECTION.search(text) is None:
        raise FlexlmError("lmstat output has no 'Feature usage info' section (truncated?)")
    usage: dict[str, FeatureUsage] = {}
    for line in text.splitlines():
        header = _USERS_OF.match(line)
        if header is None:
            continue
        counts = _COUNTS.search(header["body"])
        if counts is None:
            continue
        name = header["name"]
        prev = usage.get(name, FeatureUsage(0, 0))
        usage[name] = FeatureUsage(
            prev.issued + int(counts["issued"]), prev.in_use + int(counts["in_use"])
        )
    return usage


def parse_resources(text: str, server: str) -> dict[str, int | None]:
    """`sacctmgr -n -P show resource format=Name,Server,LastConsumed` rows of `server`:
    name -> LastConsumed (None when SLURM shows no value)."""
    resources: dict[str, int | None] = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3 or parts[1].casefold() != server.casefold():
            continue
        last = parts[2].strip()
        resources[parts[0]] = int(last) if last.isdigit() else None
    return resources


# --- configuration ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlexlmServer:
    name: str  # SLURM license server (`<feature>@<name>`)
    license: str  # FlexLM `port@host[,port@host…]`, given to `lmstat -c`
    features: tuple[str, ...]


_KEYS: Final = {"lmutil", "sacctmgr", "timeout_s", "state_file", "servers"}
_SERVER_KEYS: Final = {"license", "features"}


@dataclass(frozen=True, slots=True)
class FlexlmSyncConfig:
    servers: tuple[FlexlmServer, ...]
    lmutil: str = "lmutil"
    sacctmgr: str = "sacctmgr"
    # Per lmstat / sacctmgr call. A server costs at most 2 calls + its updates, and the
    # whole run must fit in the unit's TimeoutStartSec=50 (deploy/flexlm).
    timeout_s: float = 10.0
    state_file: Path | None = None  # warn-once memory; None: warn on every run

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> FlexlmSyncConfig:
        unknown = sorted(set(raw) - _KEYS)
        if unknown:
            raise ConfigError(f"[flexlm]: unknown keys {unknown}; known: {sorted(_KEYS)}")
        lmutil = _text(raw.get("lmutil", "lmutil"), "lmutil")
        sacctmgr = _text(raw.get("sacctmgr", "sacctmgr"), "sacctmgr")
        timeout = raw.get("timeout_s", 10.0)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ConfigError(f"[flexlm].timeout_s must be a positive number, got {timeout!r}")
        state = raw.get("state_file")
        state_file = None if state is None else Path(_text(state, "state_file"))
        servers = raw.get("servers")
        if not isinstance(servers, Mapping) or not servers:
            raise ConfigError(
                "[flexlm]: configure at least one server, e.g. [flexlm.servers.flexlm] with "
                'license = "27000@licserver" and features = ["feature"]'
            )
        return cls(
            servers=tuple(_server(name, table) for name, table in servers.items()),
            lmutil=lmutil,
            sacctmgr=sacctmgr,
            timeout_s=float(timeout),
            state_file=state_file,
        )

    def lmstat_argv(self, server: FlexlmServer) -> list[str]:
        return [self.lmutil, "lmstat", "-a", "-c", server.license]

    def show_argv(self, server: FlexlmServer) -> list[str]:
        return [
            self.sacctmgr, "-n", "-P", "show", "resource", f"server={server.name}",
            "format=Name,Server,LastConsumed",
        ]  # fmt: skip

    def update_argv(self, server: FlexlmServer, resource: str, last_consumed: int) -> list[str]:
        return [
            self.sacctmgr, "-i", "update", "resource", resource, f"server={server.name}",
            "set", f"LastConsumed={last_consumed}",
        ]  # fmt: skip


def _text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"[flexlm].{key} must be a non-empty string, got {value!r}")
    return value


def _server(name: object, table: object) -> FlexlmServer:
    where = f"[flexlm.servers.{name}]"
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ConfigError(
            f"[flexlm] server {name!r}: use the SLURM license server name (letters, digits, "
            "'_', '.', '-'), as in `<feature>@<server>`"
        )
    if not isinstance(table, Mapping):
        raise ConfigError(f"[flexlm] {where} must be a table")
    unknown = sorted(set(table) - _SERVER_KEYS)
    if unknown:
        raise ConfigError(f"[flexlm] {where}: unknown keys {unknown}; known: license, features")
    license_ = table.get("license")
    if not isinstance(license_, str) or not license_:
        raise ConfigError(f"[flexlm] {where}: license must be the FlexLM port@host")
    features = table.get("features")
    if (
        not isinstance(features, list)
        or not features
        or not all(isinstance(f, str) and _NAME_RE.match(f) for f in features)
    ):
        raise ConfigError(
            f"[flexlm] {where}: features must be a non-empty list of license feature names, "
            f"got {features!r}"
        )
    return FlexlmServer(name=name, license=license_, features=tuple(features))


# --- sync ------------------------------------------------------------------------------------

_Key = tuple[str, str]  # (server, feature)


@dataclass
class SyncReport:
    updated: dict[_Key, int] = field(default_factory=dict)  # dry run: would be updated
    unchanged: list[_Key] = field(default_factory=list)
    failed_servers: list[str] = field(default_factory=list)
    unreported: list[_Key] = field(default_factory=list)  # configured, absent from lmstat
    missing_in_slurm: list[_Key] = field(default_factory=list)
    failed_updates: list[_Key] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """False when an alert was logged; a feature unknown to SLURM is only a warning."""
        return not (self.failed_servers or self.unreported or self.failed_updates)


class FlexlmSync:
    def __init__(
        self,
        config: FlexlmSyncConfig,
        *,
        runner: CommandRunner,
        out: TextIO,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._run = runner
        self._out = out
        self._dry_run = dry_run

    def run(self) -> SyncReport:
        report = SyncReport()
        warned = self._load_state()
        missing: dict[str, list[str]] = {}
        for server in self._config.servers:
            found = self._sync_server(server, report)
            if found is None:  # unknown this time: keep what was warned about
                missing[server.name] = warned.get(server.name, [])
                continue
            missing[server.name] = found
            for feature in found:
                if feature not in warned.get(server.name, []):
                    log.warning(
                        "flexlm_feature_not_in_slurm",
                        server=server.name,
                        feature=feature,
                        hint=f"sacctmgr add resource {feature} server={server.name} "
                        "servertype=flexlm count=<issued> or remove it from [flexlm]",
                    )
        if missing != warned:
            self._save_state(missing)
        return report

    def _sync_server(self, server: FlexlmServer, report: SyncReport) -> list[str] | None:
        """Sync one server; returns its configured features unknown to SLURM, or None when
        SLURM's resources could not be listed."""
        lmstat = self._run(self._config.lmstat_argv(server), timeout_s=self._config.timeout_s)
        try:
            if lmstat.returncode != 0:
                reason = _last_line(lmstat.stderr or lmstat.stdout)
                raise FlexlmError(f"lmstat exited {lmstat.returncode}: {reason}")
            usage = parse_lmstat(lmstat.stdout)
        except FlexlmError as exc:
            log.error(
                "flexlm_server_unavailable", server=server.name, license=server.license,
                reason=str(exc), action="no LastConsumed update for this server",
            )  # fmt: skip
            report.failed_servers.append(server.name)
            return None
        show = self._run(self._config.show_argv(server), timeout_s=self._config.timeout_s)
        if show.returncode != 0:
            log.error(
                "flexlm_slurm_unavailable", server=server.name,
                reason=_last_line(show.stderr or show.stdout),
                action="no LastConsumed update for this server",
            )  # fmt: skip
            report.failed_servers.append(server.name)
            return None
        resources = parse_resources(show.stdout, server.name)
        by_fold = {name.casefold(): name for name in resources}
        usage_fold = {name.casefold(): counts for name, counts in usage.items()}
        missing: list[str] = []
        for feature in server.features:
            key = (server.name, feature)
            resource = by_fold.get(feature.casefold())
            if resource is None:
                missing.append(feature)
                report.missing_in_slurm.append(key)
                continue
            counts = usage_fold.get(feature.casefold())
            if counts is None:
                log.error(
                    "flexlm_feature_unreported", server=server.name, feature=feature,
                    action="LastConsumed left unchanged; check the feature name",
                )  # fmt: skip
                report.unreported.append(key)
                continue
            in_use = counts.in_use
            if resources[resource] == in_use:
                report.unchanged.append(key)
                continue
            self._update(server, resource, in_use, key, report)
        return missing

    def _update(
        self, server: FlexlmServer, resource: str, in_use: int, key: _Key, report: SyncReport
    ) -> None:
        argv = self._config.update_argv(server, resource, in_use)
        if self._dry_run:
            self._out.write(shlex.join(argv) + "\n")
            report.updated[key] = in_use
            return
        done = self._run(argv, timeout_s=self._config.timeout_s)
        if done.returncode != 0:
            log.error(
                "flexlm_update_failed", server=server.name, feature=resource,
                reason=_last_line(done.stderr or done.stdout),
            )  # fmt: skip
            report.failed_updates.append(key)
            return
        log.info("flexlm_last_consumed", server=server.name, feature=resource, value=in_use)
        report.updated[key] = in_use

    # warn-once state: {"missing": {server: [feature, ...]}}

    def _load_state(self) -> dict[str, list[str]]:
        path = self._config.state_file
        if path is None:
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            missing = data["missing"]
            if isinstance(missing, dict) and all(
                isinstance(v, list) and all(isinstance(f, str) for f in v) for v in missing.values()
            ):
                return {str(k): list(v) for k, v in missing.items()}
            raise ValueError("bad shape")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("flexlm_state_unreadable", path=str(path), reason=str(exc))
            return {}

    def _save_state(self, missing: dict[str, list[str]]) -> None:
        path = self._config.state_file
        if path is None or self._dry_run:
            return
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps({"missing": missing}, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("flexlm_state_unwritable", path=str(path), reason=str(exc))
            with contextlib.suppress(OSError):
                tmp.unlink()


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "(no output)"


# --- entry point -----------------------------------------------------------------------------


class _UsageExit(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageExit(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="ebs-flexlm-sync",
        description="Push FlexLM licenses in use into SLURM remote-license LastConsumed "
        "([flexlm] in ebs.toml). Run by the admin timer, never inside a SLURM job.",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="print the sacctmgr updates, change nothing"
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Exit 0: synced; 1: something needs attention (see the error events); 2: usage/config."""
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = _parser().parse_args(argv)
    except _UsageExit as exc:
        err.write(f"ebs-flexlm-sync: {exc}\n")
        return EXIT_USAGE
    if environ.get("SLURM_JOB_ID") or environ.get("SLURM_JOBID"):
        err.write(
            "ebs-flexlm-sync: refusing to run inside a SLURM job: it is an admin tool run by "
            "the ebs-flexlm-sync.timer (deploy/flexlm), never by builds\n"
        )
        return EXIT_USAGE
    configure_logging(json=True, stream=err)
    try:
        home = Path(environ.get("HOME") or Path.home())
        config = load_config(config_paths(environ, cwd=Path.cwd(), home=home))
        if "flexlm" not in config:
            raise ConfigError("no [flexlm] section in the configuration (see deploy/flexlm)")
        settings = FlexlmSyncConfig.from_mapping(config["flexlm"])
    except EbsError as exc:
        err.write(f"ebs-flexlm-sync: {exc}\n")
        return EXIT_USAGE
    sync = FlexlmSync(settings, runner=runner or SubprocessRunner(), out=out, dry_run=args.dry_run)
    return EXIT_OK if sync.run().ok else EXIT_ALERT


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
