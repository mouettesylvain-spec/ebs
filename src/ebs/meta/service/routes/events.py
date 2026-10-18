"""Event stream routes (P2-05 R2); the stream itself is `ebs.meta.service.events_stream`."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse

from ebs.meta.api import check_domain
from ebs.meta.service.auth import Principal
from ebs.meta.service.deps import ServiceDeps, current_principal, get_deps
from ebs.meta.service.events_stream import MEDIA_TYPE, stream_events
from ebs.meta.service.routes.builds import _read

_ID_MAX = 2**63 - 1  # bigint
_EVENT_ID = re.compile(r"[0-9]{1,18}")

# Authenticate before anything else (401 without credentials).
router = APIRouter(tags=["events"], dependencies=[Depends(current_principal)])

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]
After = Annotated[int | None, Query(ge=0, le=_ID_MAX, description="Resume after this event id.")]
LastEventId = Annotated[str | None, Header(alias="Last-Event-ID")]

_DOC: dict[int | str, dict[str, Any]] = {
    200: {"content": {MEDIA_TYPE: {}}, "description": "Server-Sent Events: one frame per event."}
}


def _after(query: int | None, header: str | None) -> int:
    """Where to resume: the later of `?after=` and `Last-Event-ID` (EventSource reconnects to
    the same URL with the header, which must win over the URL's start), else the beginning."""
    value = 0
    if header is not None and header.strip():
        if not _EVENT_ID.fullmatch(header.strip()):
            raise RequestValidationError(
                [{"loc": ("header", "Last-Event-ID"), "msg": f"not an event id: {header!r}"}]
            )
        value = int(header.strip())
    return max(value, query or 0)


def _response(frames: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        frames,
        media_type=MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},  # no proxy buffering
    )


@router.get("/builds/{build}/events", response_class=StreamingResponse, responses=_DOC)
def build_events(
    build: int, deps: Deps, caller: Caller, after: After = None, last_event_id: LastEventId = None
) -> StreamingResponse:
    """The events of one build, live; the stream ends when the build is finished."""
    start = _after(after, last_event_id)
    _read(deps, caller, build)  # a build of another domain does not exist (404)
    return _response(stream_events(deps, build=build, domain=None, after=start))


@router.get("/domains/{domain}/events", response_class=StreamingResponse, responses=_DOC)
def domain_events(
    domain: str,
    deps: Deps,
    caller: Caller,
    after: After = None,
    last_event_id: LastEventId = None,
) -> StreamingResponse:
    """The events of every build of a domain, live."""
    start = _after(after, last_event_id)
    deps.check(caller, "read", domain=check_domain(domain), owner=None)
    return _response(stream_events(deps, build=None, domain=domain, after=start))
