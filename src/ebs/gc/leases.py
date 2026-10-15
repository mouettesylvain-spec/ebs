"""The driver's GC lease (P1-09 R2): roots held by a running build.

At build start the driver leases the stored plan and every input id it knows; as the build goes,
it adds cache-hit and produced outputs, logs and refined plans. `heartbeat()` (called from the
scheduler loop) sends pending additions and renews the lease every `ttl_s / 3`, so a lease
outlives a missed heartbeat or two but not a dead driver (15 min). If the lease is gone (the
driver stalled past its TTL and GC expired it), a new one is acquired with everything held.

Lease failures never fail a build: GC also keeps whatever was used within `ttl_days`, so a lost
lease only narrows the protection of a build that stalls for longer than that.
"""

from __future__ import annotations

from collections.abc import Iterable

from ebs.core.clock import Clock
from ebs.core.digest import Digest
from ebs.core.errors import MetadataError
from ebs.core.log import get_logger
from ebs.meta.api import LEASE_TTL_S, BuildId, LeaseId, MetadataStore

__all__ = ["LeaseKeeper"]

_log = get_logger(__name__)


class LeaseKeeper:
    """Acquires, extends and renews one build's lease. Single-threaded, like the scheduler."""

    def __init__(
        self,
        store: MetadataStore,
        domain: str,
        build: BuildId,
        clock: Clock,
        *,
        ttl_s: int = LEASE_TTL_S,
    ) -> None:
        self._store = store
        self._domain = domain
        self._build = build
        self._clock = clock
        self._ttl_s = ttl_s
        self._lease: LeaseId | None = None
        self._held: set[Digest] = set()
        self._pending: set[Digest] = set()
        self._renewed_at = 0.0

    def start(self, digests: Iterable[Digest]) -> None:
        """Acquire the lease on `digests`."""
        self._held |= set(digests)
        self._acquire()

    def add(self, digests: Iterable[Digest]) -> None:
        """Protect more objects; sent on the next heartbeat."""
        new = set(digests) - self._held
        self._held |= new
        self._pending |= new

    def heartbeat(self) -> None:
        """Send pending additions, and renew the lease if a third of its TTL has passed."""
        if self._lease is None and not self._held:
            return
        due = self._clock.monotonic() - self._renewed_at >= self._ttl_s / 3
        if self._lease is None:
            self._acquire()
            return
        if not (due or self._pending):
            return
        try:
            self._store.renew_lease(self._build, self._lease, self._ttl_s, add=self._pending)
        except MetadataError as exc:
            _log.warning("driver.lease_renew_failed", build=self._build, error=str(exc))
            self._acquire()
            return
        self._pending.clear()
        self._renewed_at = self._clock.monotonic()

    def _acquire(self) -> None:
        try:
            self._lease = self._store.acquire_lease(
                self._domain, self._build, sorted(self._held, key=str), self._ttl_s
            )
        except MetadataError as exc:
            self._lease = None
            _log.warning("driver.lease_acquire_failed", build=self._build, error=str(exc))
            return
        self._pending.clear()
        self._renewed_at = self._clock.monotonic()
