"""Pilot queue routes (P3-06), scoped by domain: the driver queues actions, pilots lease them.

Every route needs membership of the domain (P2-01: 403 otherwise). Writes are `queue_write`: any
member, like the action cache; a pool is addressed by the random UUID its executor drew, and its
pilots run as the same user as its driver.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from ebs.meta.api import (
    Changed,
    OptionalQueueLease,
    QueueEntries,
    QueueFinish,
    QueueIds,
    QueueLeaseRequest,
    QueuePut,
    QueueRenew,
    check_domain,
)
from ebs.meta.service.auth import Operation, Principal
from ebs.meta.service.deps import ServiceDeps, body, current_principal, get_deps, json_body
from ebs.meta.service.routes import no_content, ok

# Authenticate before any body is read or validated (401, never 422, without credentials).
router = APIRouter(
    prefix="/domains/{domain}/pilot-queues/{pool}",
    tags=["pilot"],
    dependencies=[Depends(current_principal)],
)

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]


def _check(deps: ServiceDeps, caller: Principal, domain: str, op: Operation) -> None:
    deps.check(caller, op, domain=check_domain(domain), owner=None)


@router.post("/items", response_model=QueueIds, openapi_extra=json_body(QueuePut))
def queue_put(
    domain: str,
    pool: UUID,
    req: Annotated[QueuePut, Depends(body(QueuePut))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _check(deps, caller, domain, "queue_write")
    return ok(QueueIds(ids=deps.store.queue_put(domain, pool, req.items)))


@router.post(
    "/lease", response_model=OptionalQueueLease, openapi_extra=json_body(QueueLeaseRequest)
)
def queue_lease(
    domain: str,
    pool: UUID,
    req: Annotated[QueueLeaseRequest, Depends(body(QueueLeaseRequest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _check(deps, caller, domain, "queue_write")
    return ok(OptionalQueueLease(lease=deps.store.queue_lease(domain, pool, req.slot, req.ttl_s)))


@router.post("/items/{item}/renew", response_model=Changed, openapi_extra=json_body(QueueRenew))
def queue_renew(
    domain: str,
    pool: UUID,
    item: int,
    req: Annotated[QueueRenew, Depends(body(QueueRenew))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _check(deps, caller, domain, "queue_write")
    renewed = deps.store.queue_renew(domain, pool, item, req.pilot, req.ttl_s)
    return ok(Changed(changed=renewed))


@router.post("/items/{item}/finish", response_model=Changed, openapi_extra=json_body(QueueFinish))
def queue_finish(
    domain: str,
    pool: UUID,
    item: int,
    req: Annotated[QueueFinish, Depends(body(QueueFinish))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _check(deps, caller, domain, "queue_write")
    done = deps.store.queue_finish(domain, pool, item, req.pilot, req.exit_code, req.infra_reason)
    return ok(Changed(changed=done))


@router.post("/status", response_model=QueueEntries, openapi_extra=json_body(QueueIds))
def queue_status(
    domain: str,
    pool: UUID,
    req: Annotated[QueueIds, Depends(body(QueueIds))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _check(deps, caller, domain, "read")
    return ok(QueueEntries(entries=deps.store.queue_status(domain, pool, req.ids)))


@router.post("/cancel", status_code=204, openapi_extra=json_body(QueueIds))
def queue_cancel(
    domain: str,
    pool: UUID,
    req: Annotated[QueueIds, Depends(body(QueueIds))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _check(deps, caller, domain, "queue_write")
    deps.store.queue_cancel(domain, pool, req.ids)
    return no_content()
