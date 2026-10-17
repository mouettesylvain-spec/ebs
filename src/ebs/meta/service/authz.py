"""Domain authorization of the metadata service (P2-01 R1, R2, R5; invariant I20).

`DomainPolicy` is the `authorize` hook of a deployed service. It wraps the owner rules of
`auth.Policy` and first checks the domain of the request (the route's domain, the domain of the
build it names, or both):

- the service serves only the domains listed in `[meta_service].domains` (its service account
  is a member of exactly those, R5), each configured in `[[domains]]`;
- a MUNGE user must be in the domain's `unix_group` (NSS/SSSD, `NssGroups`, cached for a TTL);
- a bearer token's principal is scoped by `[meta_service].token_domains` (`{"ci-cpu" =
  ["cpu-nda"]}`), else by the NSS groups of a user of that name (a team's service account).

A refusal raises `DomainDenied` (HTTP 403). Routes that address an object by id answer 404
instead, and cache reads answer "miss", so a non-member cannot even learn that something exists.
"""

from __future__ import annotations

import grp
import os
import pwd
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Protocol

from ebs.core.clock import Clock, SystemClock
from ebs.core.errors import ConfigError
from ebs.core.log import get_logger
from ebs.domains import Domains
from ebs.meta.service.auth import (
    AuthorizeHook,
    BuildOwner,
    Operation,
    PermissionDeniedError,
    Principal,
)

__all__ = ["DomainDenied", "DomainPolicy", "GroupResolver", "NssGroups"]

_log = get_logger(__name__)


class DomainDenied(PermissionDeniedError):
    """The caller is not a member of the request's domain (or the service does not serve it)."""

    def __init__(self, message: str, domain: str) -> None:
        super().__init__(message)
        self.domain = domain


class GroupResolver(Protocol):
    def groups_of(self, user: str) -> frozenset[str]:
        """The names of `user`'s groups (primary included); empty for an unknown user."""
        ...


class NssGroups:
    """Group membership through NSS (`getgrouplist`, so LDAP/SSSD groups count), cached per user
    for `ttl_s` seconds: a removed member loses access within the TTL."""

    def __init__(
        self,
        *,
        ttl_s: float = 60.0,
        clock: Clock | None = None,
        getpwnam: Callable[[str], pwd.struct_passwd] = pwd.getpwnam,
        getgrouplist: Callable[[str, int], list[int]] = os.getgrouplist,
        getgrgid: Callable[[int], grp.struct_group] = grp.getgrgid,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock or SystemClock()
        self._getpwnam = getpwnam
        self._getgrouplist = getgrouplist
        self._getgrgid = getgrgid
        self._cache: dict[str, tuple[float, frozenset[str]]] = {}
        self._lock = threading.Lock()

    def _lookup(self, user: str) -> frozenset[str]:
        try:
            gids = self._getgrouplist(user, self._getpwnam(user).pw_gid)
        except (KeyError, OSError):
            return frozenset()
        names = set()
        for gid in gids:
            try:
                names.add(self._getgrgid(gid).gr_name)
            except KeyError:
                continue
        return frozenset(names)

    def groups_of(self, user: str) -> frozenset[str]:
        now = self._clock.monotonic()
        with self._lock:
            cached = self._cache.get(user)
            if cached is not None and cached[0] > now:
                return cached[1]
        found = self._lookup(user)
        with self._lock:
            self._cache[user] = (now + self._ttl_s, found)
        return found


class DomainPolicy:
    """The deployed `authorize` hook: domain membership first, then `base` (owner rules)."""

    def __init__(
        self,
        base: AuthorizeHook,
        domains: Domains,
        served: Iterable[str],
        groups: GroupResolver,
        token_domains: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self.base = base
        self.domains = domains
        self.served = frozenset(served)
        if not self.served:
            raise ConfigError(
                "[meta_service].domains must list the domains this service serves (its service "
                "account is a member of exactly those)"
            )
        for name in sorted(self.served):
            domains.require(name)
        self.groups = groups
        self.token_domains = {k: frozenset(v) for k, v in (token_domains or {}).items()}
        for principal, names in sorted(self.token_domains.items()):
            unknown = names - self.served
            if unknown:
                raise ConfigError(
                    f"[meta_service].token_domains {principal!r}: {sorted(unknown)} not served "
                    "by this service ([meta_service].domains)"
                )

    def member_of(self, principal: Principal) -> frozenset[str]:
        """The served domains `principal` may use."""
        if principal.via == "token" and principal.name in self.token_domains:
            return self.token_domains[principal.name]
        groups = self.groups.groups_of(principal.name)
        return frozenset(
            name for name in self.served if self.domains.require(name).unix_group in groups
        )

    def check_domain(self, principal: Principal, domain: str) -> None:
        if domain not in self.served:
            raise DomainDenied(f"domain {domain!r} is not served by this metadata service", domain)
        if domain not in self.member_of(principal):
            _log.info("domain access refused", principal=principal.name, domain=domain)
            raise DomainDenied(
                f"{principal.name!r} is not a member of domain {domain!r} (Unix group "
                f"{self.domains.require(domain).unix_group!r}); ask the domain admins",
                domain,
            )

    def __call__(
        self,
        principal: Principal,
        op: Operation,
        *,
        domain: str | None,
        owner: BuildOwner | None,
    ) -> None:
        for name in sorted({d for d in (domain, owner.domain if owner else None) if d}):
            self.check_domain(principal, name)
        self.base(principal, op, domain=domain, owner=owner)
