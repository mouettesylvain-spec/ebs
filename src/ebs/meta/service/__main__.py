"""Admin entry point of the metadata service: `python -m ebs.meta.service <command>`.

    serve [--host H] [--port P]                         run the service (uvicorn, from ebs.toml)
    token create NAME --kind ci|service|user [--team T] [--expires-days N]
    token revoke NAME                                   revoke every token of NAME
    events prune [--dry-run]                            event retention (P2-05 R6)

`token create` prints the new token once; only its hash is stored. `events prune` drops the
months of events older than `[meta_service.events].retention_days` (default 180) and creates
next month's partition; run it daily (deploy/meta/systemd/ebs-meta-prune.timer). Commands that
need the database read `[metadata].url` (postgresql) from ebs.toml like the service does.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, TextIO

from ebs.config import config_paths, load_config
from ebs.core.clock import Clock, SystemClock
from ebs.core.errors import ConfigError, EbsError
from ebs.meta.service.tokens import TokenKind

__all__ = ["main"]


class _Tokens(Protocol):
    def add(
        self,
        principal: str,
        *,
        kind: TokenKind,
        team: str | None = ...,
        expires_at: datetime | None = ...,
    ) -> str: ...

    def revoke(self, principal: str) -> int: ...


class _Events(Protocol):
    def prune_events(self, before: datetime, *, dry_run: bool = False) -> list[str]: ...

    def ensure_event_partitions(self, at: datetime, *, months_ahead: int = 1) -> list[str]: ...


def _config(environ: Mapping[str, str]) -> Mapping[str, object]:
    home = Path(environ.get("HOME") or Path.home())
    return load_config(config_paths(environ, cwd=Path.cwd(), home=home))


def _pg_tokens(environ: Mapping[str, str]) -> _Tokens:
    from ebs.meta.pg import PgMetadataStore
    from ebs.meta.service.tokens import PgTokenStore

    store = PgMetadataStore.from_config(_config(environ).get("metadata", {}))  # type: ignore[arg-type]
    return PgTokenStore(store.engine)


def _pg_events(environ: Mapping[str, str]) -> tuple[_Events, int]:
    """The store and `[meta_service.events].retention_days`."""
    from ebs.meta.pg import PgMetadataStore
    from ebs.meta.service.app import ServiceConfig

    config = _config(environ)
    service = ServiceConfig.from_mapping(config.get("meta_service", {}))  # type: ignore[arg-type]
    store = PgMetadataStore.from_config(config.get("metadata", {}))  # type: ignore[arg-type]
    return store, service.events.retention_days


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ebs.meta.service", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the metadata service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    token = sub.add_parser("token", help="manage bearer tokens")
    tsub = token.add_subparsers(dest="token_command", required=True)
    create = tsub.add_parser("create", help="create a token and print it once")
    create.add_argument("name")
    create.add_argument("--kind", choices=["ci", "service", "user"], required=True)
    create.add_argument("--team", help="project a CI token may write builds for")
    create.add_argument("--expires-days", type=int, default=None)
    revoke = tsub.add_parser("revoke", help="revoke every token of NAME")
    revoke.add_argument("name")
    events = sub.add_parser("events", help="the events table")
    esub = events.add_subparsers(dest="events_command", required=True)
    prune = esub.add_parser("prune", help="drop months of events older than the retention")
    prune.add_argument("--dry-run", action="store_true", help="list them, drop nothing")
    return p


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    tokens: Callable[[Mapping[str, str]], _Tokens] = _pg_tokens,
    events: Callable[[Mapping[str, str]], tuple[_Events, int]] = _pg_events,
    clock: Clock | None = None,
    out: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    stdout = out or sys.stdout
    try:
        if args.command == "serve":  # pragma: no cover - runs a server until stopped
            import uvicorn

            uvicorn.run(
                "ebs.meta.service.app:app_from_config", factory=True, host=args.host,
                port=args.port, proxy_headers=True,
            )  # fmt: skip
            return 0
        if args.command == "events":
            log, retention_days = events(env)
            now = (clock or SystemClock()).now()
            months = log.prune_events(now - timedelta(days=retention_days), dry_run=args.dry_run)
            verb = "would drop" if args.dry_run else "dropped"
            stdout.write(f"{verb} {', '.join(months) if months else 'nothing'}\n")
            if not args.dry_run:
                log.ensure_event_partitions(now)
            return 0
        if args.token_command == "create" and args.kind == "ci" and not args.team:
            raise ConfigError("a ci token needs --team: the project whose builds it writes")
        store = tokens(env)
        if args.token_command == "create":
            expires = None
            if args.expires_days is not None:
                expires = (clock or SystemClock()).now() + timedelta(days=args.expires_days)
            token = store.add(args.name, kind=args.kind, team=args.team, expires_at=expires)
            stdout.write(f"{token}\n")
            return 0
        count = store.revoke(args.name)
        stdout.write(f"revoked {count} token(s) of {args.name}\n")
        return 0
    except EbsError as exc:
        sys.stderr.write(f"ebs-meta: {exc}\n")
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
