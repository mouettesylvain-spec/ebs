"""`ebs logs ACTION`: the tool log of one action (R4).

A finished action's log is the blob its ResultManifest names (`get_result`), or the `log` of
its last `infra_failed` event. A running action's log lives in the runner's scratch dir
`<scratch>/<build id>/<action dir>/logs/` (local executor; grid streaming is D2/P1-04): the
runner writes `tool.log.cur`, renames it to `tool.log.prev` when the size cap is reached and
starts a new `.cur`, and only at the end joins them into `tool.log` (`ebs.runner.run`).

`-f` keeps the `.cur` file open: a rename does not disturb an open handle, so bytes are read
once, in order, also across the final rename to `tool.log`. A new `.cur` (another inode) means a
rotation (the old file is now `.prev`) or a retry (the old scratch dir was deleted): the old
handle is read to its end first, then the new file from its start, with a separator line for
a retry. Limits, both only once a log reaches the size cap (`[runner].max_log`, 2 GiB by
default): two rotations within one poll interval skip the middle file, and a rotation in the
last interval before the tool ends hides that interval's tail (the runner joins and deletes the
files at once); `ebs logs ACTION` afterwards prints the kept log. `-f` stops when the action
reaches a final state or the build ends; an action that never wrote while followed gets its
stored log. ACTION is an action id or a unique prefix.
"""

from __future__ import annotations

import codecs
import os
import sys
from pathlib import Path
from typing import BinaryIO, Final

import typer

from ebs.cli._context import SiteServices
from ebs.cli._output import UsageError, handle_errors, make_output
from ebs.cli._records import FINAL_STATES, BuildRecord, action_states, build_status, find_build
from ebs.cli.plan import JSON_OPTION
from ebs.core.digest import Digest
from ebs.core.errors import ConfigError
from ebs.driver.events import Event
from ebs.runner.stage import action_dir_name

__all__ = ["logs_command", "match_action"]

_CHUNK: Final = 1 << 16
LOG_NAME: Final = "tool.log"  # ebs.runner.main: scratch.logs / "tool.log"


def match_action(given: str, actions: dict[str, str]) -> str:
    """The action id `given` names: itself, or the only id it is a prefix of."""
    if given in actions:
        return given
    matches = [a for a in actions if a.startswith(given)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ConfigError(f"no action {given!r} in this build; `ebs status` lists the steps")
    shown = ", ".join(matches[:10]) + (", …" if len(matches) > 10 else "")
    raise ConfigError(f"{given!r} matches several actions ({shown}); give more of the id")


class _Reader:
    def __init__(self, services: SiteServices, record: BuildRecord, action_id: str) -> None:
        self.services = services
        self.record = record
        self.action_id = action_id

    def events(self) -> list[Event]:
        return self.record.events(self.services.cwd)

    def state(self, events: list[Event]) -> str:
        return action_states([self.action_id], events)[self.action_id]

    def done(self, events: list[Event]) -> bool:
        if self.state(events) in FINAL_STATES:
            return True
        return build_status(self.record, events) != "running"

    def logs_dir(self) -> Path:
        scratch = self.services.runner_settings().scratch_dir
        return scratch / str(self.record.build) / action_dir_name(self.action_id) / "logs"

    def live(self) -> Path:
        return self.logs_dir() / f"{LOG_NAME}.cur"

    def scratch_bytes(self) -> bytes | None:
        """What the running tool wrote so far (`.prev` + `.cur`, or the joined `tool.log`)."""
        logs = self.logs_dir()
        joined = logs / LOG_NAME
        parts = [joined] if joined.exists() else [logs / f"{LOG_NAME}.prev", self.live()]
        data = [p.read_bytes() for p in parts if p.exists()]
        return b"".join(data) if data else None

    def stored_log(self, events: list[Event]) -> bytes | None:
        """The log blob of the action's result, else of its last infra failure."""
        digest: Digest | None = None
        store = self.services.store(required=False)
        if store is not None:
            result = store.get_result(self.record.build, self.action_id)
            digest = result.log if result is not None else None
        if digest is None:
            for event in reversed(events):
                if event.action_id == self.action_id and event.type == "infra_failed":
                    raw = event.data.get("log")
                    digest = Digest.parse(raw) if isinstance(raw, str) else None
                    break
        if digest is None:
            if store is None:
                raise ConfigError(
                    f"the log of {self.action_id} is in the metadata store's result: "
                    "set [metadata].url in ebs.toml"
                )
            return None
        with self.services.cas(self.record.domain).open(digest) as f:
            return f.read()


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


class _Follower:
    def __init__(self, reader: _Reader) -> None:
        self.reader = reader
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.handle: BinaryIO | None = None
        self.opened = False  # a live file was seen at least once

    def drain(self) -> None:
        if self.handle is not None:
            while chunk := self.handle.read(_CHUNK):
                _write(self.decoder.decode(chunk))

    def switch(self) -> None:
        """Open the current `.cur` if it is not the file already open."""
        try:
            new = self.reader.live().open("rb")
        except FileNotFoundError:
            return
        if self.handle is not None:
            if os.fstat(new.fileno()).st_ino == os.fstat(self.handle.fileno()).st_ino:
                new.close()
                return
            self.drain()
            retried = os.fstat(self.handle.fileno()).st_nlink == 0  # its scratch dir is gone
            self.handle.close()
            if retried:
                _write(self.decoder.decode(b"", final=True))
                _write(f"--- ebs: {self.reader.action_id} restarted (new attempt) ---\n")
        self.handle = new
        self.opened = True

    def run(self, interval: float) -> None:
        try:
            while True:
                # The state is read before the file: bytes written before a final event are
                # then always in the file when we read it.
                events = self.reader.events()
                done = self.reader.done(events)
                self.drain()
                self.switch()
                self.drain()
                if done:
                    if not self.opened:  # finished before we looked
                        data = self.reader.stored_log(events) or self.reader.scratch_bytes()
                        _write(self.decoder.decode(data or b""))
                    _write(self.decoder.decode(b"", final=True))
                    return
                self.reader.services.clock.sleep(interval)
        finally:
            if self.handle is not None:
                self.handle.close()


@handle_errors
def logs_command(
    ctx: typer.Context,
    action: str = typer.Argument(..., metavar="ACTION", help="Action id or a unique prefix."),
    follow: bool = typer.Option(
        False, "-f", "--follow", help="Print the log as it grows until the action ends."
    ),
    build: str | None = typer.Option(
        None, "--build", metavar="BUILD", help="Build UUID (or prefix) or id; default: the latest."
    ),
    interval: float = typer.Option(1.0, "--interval", min=0.1, help="Seconds between checks (-f)."),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Print an action's tool log (from the CAS when finished, live while running)."""
    if follow and json_mode:
        raise UsageError("-f prints the log as text; leave out --json to follow it")
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    record = find_build(services.cwd, build)
    reader = _Reader(services, record, match_action(action, record.actions))
    if follow:
        _Follower(reader).run(interval)
        return
    events = reader.events()
    state = reader.state(events)
    data = None
    if state in FINAL_STATES or state == "infra_failed":
        data = reader.stored_log(events)
        if data is None:
            raise ConfigError(f"no log was recorded for {reader.action_id} (state {state})")
    else:
        data = reader.scratch_bytes()
        if data is None:
            raise ConfigError(
                f"{reader.action_id} has not started yet in build {record.uuid} "
                f"(state {state}); use -f to wait for its log"
            )
    text = data.decode("utf-8", errors="replace")
    if json_mode:
        out.emit(
            {
                "action_id": reader.action_id,
                "build": int(record.build),
                "uuid": str(record.uuid),
                "state": state,
                "log": text,
            }
        )
    else:
        _write(text)
