"""The I20 matrix (P2-01 R1, R2) over the in-memory store, so `make check` catches a route that
answers without authorizing; tests/integration/test_authz_matrix.py runs it on PostgreSQL."""

from __future__ import annotations

from ebs.core.clock import FakeClock
from ebs.meta.memory import InMemoryMetadataStore
from tests.helpers.authz_matrix import covers_every_route, run_matrix


# R1
def test_matrix_covers_every_route() -> None:
    covers_every_route(InMemoryMetadataStore(clock=FakeClock()))


# R1, R2 (I20)
def test_authz_matrix() -> None:
    run_matrix(InMemoryMetadataStore(clock=FakeClock()))
