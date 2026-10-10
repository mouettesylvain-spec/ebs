"""Bearer tokens for CI and services (P1-06 R3): only their SHA-256 is stored.

A token is shown once when created (`python -m ebs.meta.service token create`). `lookup` returns
only usable tokens: not revoked and not expired by the store's clock.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol

import sqlalchemy as sa

from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import hash_bytes
from ebs.core.errors import MetadataError
from ebs.meta.models import api_tokens

__all__ = [
    "InMemoryTokenStore",
    "PgTokenStore",
    "TokenInfo",
    "TokenKind",
    "TokenStore",
    "hash_token",
    "new_token",
]

TokenKind = Literal["user", "ci", "service"]


@dataclass(frozen=True)
class TokenInfo:
    principal: str
    kind: TokenKind
    team: str | None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    def usable(self, now: datetime) -> bool:
        return self.revoked_at is None and (self.expires_at is None or now < self.expires_at)


def hash_token(token: str) -> str:
    """The stored form of a token (`sha256:<hex>`)."""
    return str(hash_bytes(token.encode("utf-8")))


def new_token() -> str:
    return "ebs_" + secrets.token_urlsafe(32)


def _check(principal: str, kind: str) -> None:
    if not principal or not principal.isprintable() or len(principal) > 256:
        raise MetadataError(f"invalid token principal {principal!r}")
    if kind not in ("user", "ci", "service"):
        raise MetadataError(f"invalid token kind {kind!r}: expected user, ci or service")


class TokenStore(Protocol):
    def lookup(self, token_hash: str) -> TokenInfo | None:
        """The usable token with this hash, or None."""
        ...


class InMemoryTokenStore:
    """Tokens in memory, for tests and development."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._by_hash: dict[str, TokenInfo] = {}
        self._lock = threading.Lock()

    def add(
        self,
        principal: str,
        *,
        kind: TokenKind,
        team: str | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        _check(principal, kind)
        token = new_token()
        with self._lock:
            self._by_hash[hash_token(token)] = TokenInfo(principal, kind, team, expires_at)
        return token

    def revoke(self, principal: str) -> int:
        now = self._clock.now()
        with self._lock:
            hits = [h for h, t in self._by_hash.items() if t.principal == principal]
            for h in hits:
                self._by_hash[h] = replace(self._by_hash[h], revoked_at=now)
        return len(hits)

    def lookup(self, token_hash: str) -> TokenInfo | None:
        with self._lock:
            info = self._by_hash.get(token_hash)
        return info if info is not None and info.usable(self._clock.now()) else None


class PgTokenStore:
    """Tokens in `ebs.api_tokens` (migration 0004)."""

    def __init__(self, engine: sa.Engine, *, clock: Clock | None = None) -> None:
        self._engine = engine
        self._clock = clock or SystemClock()

    def add(
        self,
        principal: str,
        *,
        kind: TokenKind,
        team: str | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        _check(principal, kind)
        token = new_token()
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    sa.insert(api_tokens).values(
                        principal=principal,
                        kind=kind,
                        team=team,
                        hash=hash_token(token),
                        created_at=self._clock.now(),
                        expires_at=expires_at,
                    )
                )
        except sa.exc.SQLAlchemyError as exc:
            raise MetadataError(f"cannot store the token for {principal!r}: {exc}") from exc
        return token

    def revoke(self, principal: str) -> int:
        t = api_tokens
        try:
            with self._engine.begin() as conn:
                done = conn.execute(
                    sa.update(t)
                    .where(t.c.principal == principal, t.c.revoked_at.is_(None))
                    .values(revoked_at=self._clock.now())
                )
        except sa.exc.SQLAlchemyError as exc:
            raise MetadataError(f"cannot revoke the tokens of {principal!r}: {exc}") from exc
        return int(done.rowcount)

    def lookup(self, token_hash: str) -> TokenInfo | None:
        t = api_tokens
        now = self._clock.now()
        stmt = sa.select(t.c.principal, t.c.kind, t.c.team, t.c.expires_at, t.c.revoked_at).where(
            t.c.hash == token_hash,
            t.c.revoked_at.is_(None),
            sa.or_(t.c.expires_at.is_(None), t.c.expires_at > now),
        )
        try:
            with self._engine.begin() as conn:
                row = conn.execute(stmt).first()
        except sa.exc.SQLAlchemyError as exc:
            raise MetadataError(f"token lookup failed: metadata database error: {exc}") from exc
        if row is None:
            return None
        return TokenInfo(row[0], row[1], row[2], row[3], row[4])
