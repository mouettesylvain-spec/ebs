"""Dashboard reads of a domain (P2-06): its builds and its cache and pending-wait statistics.

    GET /domains/{domain}/builds   newest first; filters user, status, project, since, until
    GET /domains/{domain}/stats    cache hits, saved CPU time, pending waits of a window

Both are authorized as a `read` of the domain (403 for non-members, R7): the service, not the
dashboard, decides what a caller sees.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from ebs.meta.api import (
    STATS_WINDOW_MAX_DAYS,
    BuildFilter,
    BuildStatus,
    BuildViewList,
    DomainStats,
    check_domain,
)
from ebs.meta.service.auth import Principal
from ebs.meta.service.deps import ServiceDeps, current_principal, get_deps
from ebs.meta.service.pagination import Cursor, Limit, paginate
from ebs.meta.service.routes import ok

__all__ = ["DEFAULT_STATS_DAYS", "router"]

DEFAULT_STATS_DAYS = 30

# Authenticate before anything else (401 without credentials).
router = APIRouter(tags=["dashboard"], dependencies=[Depends(current_principal)])

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]
When = Annotated[datetime | None, Query(description="ISO 8601 with a time zone.")]


def _invalid(field: str, msg: str) -> RequestValidationError:
    return RequestValidationError([{"loc": ("query", field), "msg": msg}])


def _aware(field: str, value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise _invalid(field, "give a time zone (e.g. 2026-10-01T00:00:00Z)")
    return value


@router.get("/domains/{domain}/builds", response_model=BuildViewList)
def list_builds(
    domain: str,
    deps: Deps,
    caller: Caller,
    user: str | None = None,
    status: BuildStatus | None = None,
    project: str | None = None,
    since: When = None,
    until: When = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    deps.check(caller, "read", domain=check_domain(domain), owner=None)
    try:
        flt = BuildFilter(
            user=user,
            status=status,
            project=project,
            since=_aware("since", since),
            until=_aware("until", until),
        )
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from None
    page, after = paginate(
        deps.store.list_builds(domain, flt),
        lambda b: (-int(b.id),),  # newest first
        cursor=cursor,
        limit=limit,
        max_page_size=deps.limits.max_page_size,
        query={"list": "builds", "domain": domain, **flt.model_dump(mode="json")},
    )
    return ok(BuildViewList(builds=page, next=after))


@router.get("/domains/{domain}/stats", response_model=DomainStats)
def domain_stats(
    domain: str, deps: Deps, caller: Caller, since: When = None, until: When = None
) -> Response:
    """The builds created in `since..until` (default: the last 30 days)."""
    deps.check(caller, "read", domain=check_domain(domain), owner=None)
    end = _aware("until", until) or deps.clock.now()
    start = _aware("since", since) or end - timedelta(days=DEFAULT_STATS_DAYS)
    if not start < end <= start + timedelta(days=STATS_WINDOW_MAX_DAYS):
        raise _invalid("since", f"since must be before until, at most {STATS_WINDOW_MAX_DAYS} days")
    return ok(deps.store.domain_stats(domain, since=start, until=end))
