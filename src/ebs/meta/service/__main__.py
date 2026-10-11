"""Admin entry point of the metadata service: `python -m ebs.meta.service <command>`.

    serve [--host H] [--port P]                         run the service (uvicorn, from ebs.toml)
    token create NAME --kind ci|service|user [--team T] [--expires-days N]
    token revoke NAME                                   revoke every token of NAME

`token create` prints the new token once; only its hash is stored. Commands that need the
database read `[metadata].url` (postgresql) from ebs.toml like the service does.
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


def _pg_tokens(environ: Mapping[str, str]) -> _Tokens:
    from ebs.meta.pg import PgMetadataStore
    from ebs.meta.service.tokens import PgTokenStore

    home = Path(environ.get("HOME") or Path.home())
    config = load_config(config_paths(environ, cwd=Path.cwd(), home=home))
    store = PgMetadataStore.from_config(config.get("metadata", {}))
    return PgTokenStore(store.engine)


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
    return p


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    tokens: Callable[[Mapping[str, str]], _Tokens] = _pg_tokens,
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
