"""`HttpMetadataStore`: the MetadataStore client of the metadata service (P1-06).

Runners on compute nodes and CLIs on login nodes use it instead of database credentials. Every
call is one request under `API_PREFIX` whose body and response are `ebs.meta.api` models; the
server applies the same store, so behaviour matches `PgMetadataStore` (contract suite, R1).

Transport (R5): each request times out after 10 s. A request that never reached the server
(connect error) is retried for every call; 5xx answers and read timeouts are retried only for
idempotent calls (reads, `cache_put`, `record_result`, `touch`, `cache_get_many`), with
exponential backoff. Each attempt gets fresh credentials, since MUNGE refuses replays.

Batches (R2): `add_actions`, `touch` and `cache_get_many` send at most `BATCH_MAX` items per
request. `add_actions` checks duplicate ids over the whole sequence before sending; each batch is
atomic, but a failure in a later batch leaves the earlier ones added (interfaces.md § 6).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol, TypeVar
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from pydantic_core import PydanticSerializationError

from ebs.core.digest import Digest
from ebs.core.errors import ConfigError, MetadataError
from ebs.core.log import get_logger
from ebs.meta._common import (
    check_blobs,
    check_digest,
    check_driver_owner,
    check_lease_ttl,
    check_pin,
    check_toolchain,
    check_walk,
    revalidate,
)
from ebs.meta.api import (
    API_PREFIX,
    BATCH_MAX,
    PROVENANCE_START_MAX,
    ActionBatch,
    ActionList,
    ActionRow,
    ActionState,
    BlobBatch,
    BlobRef,
    BuildCreate,
    BuildFinish,
    BuildId,
    BuildList,
    BuildStatus,
    BuildView,
    CacheHits,
    Changed,
    ChannelHistory,
    ChannelList,
    ChannelMove,
    ChannelSet,
    ChannelView,
    CreatedBuild,
    CreatedLease,
    DigestBatch,
    DriverClaim,
    DriverLease,
    DriverRelease,
    Event,
    ExportList,
    ExportRecord,
    ExportRequest,
    GateCheck,
    GateQuery,
    LeaseId,
    LeaseRequest,
    OptionalDigest,
    OptionalResult,
    PinRequest,
    ProvenanceGraph,
    ProvenanceQuery,
    RegisterOutcome,
    ReleaseCreate,
    ReleaseId,
    ReleaseImport,
    ReleaseImportBatch,
    ReleaseImportList,
    ReleaseList,
    ReleaseView,
    ResultManifest,
    StateChange,
    ToolchainCreate,
    ToolchainList,
    ToolchainManifest,
    ToolchainRecord,
    ToolchainRegistration,
    YankRequest,
    check_domain,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MUNGE_HEADER",
    "MUNGE_PAYLOAD_VERSION",
    "TOKEN_ENV",
    "BearerCredential",
    "CredentialSource",
    "HttpMetadataConfig",
    "HttpMetadataStore",
    "MetadataHttpError",
    "MungeCredential",
    "munge_payload",
]

_log = get_logger(__name__)

DEFAULT_TIMEOUT_S: Final = 10.0
MUNGE_HEADER: Final = "X-EBS-Munge"
TOKEN_ENV: Final = "EBS_META_TOKEN"
"""Environment variable holding a bearer token (CI jobs); `[metadata].token_file` is the other
way to give one."""

_RETRY_STATUS: Final = frozenset({500, 502, 503, 504})
_BACKOFF_CAP_S: Final = 8.0
_RETRY_AFTER_CAP_S: Final = 60.0

_M = TypeVar("_M", bound=BaseModel)
_P = TypeVar(
    "_P", ActionList, ToolchainList, ReleaseList, ChannelHistory, ExportList, ReleaseImportList
)
_T = TypeVar("_T")


def _retry_after(response: httpx.Response) -> float | None:
    """A 429's `Retry-After` in seconds (capped), or None if it gives none we can use."""
    value = response.headers.get("Retry-After", "").strip()
    if not value.isdigit():
        return None
    return min(float(value), _RETRY_AFTER_CAP_S)


class MetadataHttpError(MetadataError):
    """The service refused a request (4xx); `status` is the HTTP status (P2-01: 403 not a
    member, 404 no such object, 409 a store rule)."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class CredentialSource(Protocol):
    """Headers that authenticate one request; called again for every attempt.

    `method` and `path` (below `API_PREFIX`, percent-decoded) let a credential be bound to the
    request it authenticates (MUNGE).
    """

    def headers(self, method: str, path: str) -> dict[str, str]: ...


MUNGE_PAYLOAD_VERSION: Final = "ebs-meta/1"


def munge_payload(audience: str, method: str, path: str) -> str:
    """What a MUNGE credential for this service must carry: compact JSON
    `["ebs-meta/1", audience, METHOD, path]`, with `path` below `API_PREFIX`, percent-decoded.

    MUNGE credentials are only replay-protected per host, so one seen in other MUNGE traffic of
    the cluster (SLURM RPCs) could otherwise be presented here; binding the payload to this
    service (`audience`) and to this request makes such a credential useless.
    """
    return json.dumps(
        [MUNGE_PAYLOAD_VERSION, audience, method.upper(), path],
        separators=(",", ":"),
        ensure_ascii=True,
    )


class BearerCredential:
    """`Authorization: Bearer <token>` for CI and services (D1)."""

    def __init__(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise ConfigError("the metadata service token is empty")
        self._token = token

    def headers(self, method: str, path: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def __repr__(self) -> str:
        return "BearerCredential(***)"


class _Run(Protocol):
    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


def _run(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # argv list, no shell
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )


class MungeCredential:
    """A fresh MUNGE credential per request, sent as `X-EBS-Munge` (D1).

    MUNGE proves the caller's uid to the service; munged on the login and compute nodes must
    share the service host's key. The credential carries `munge_payload(audience, method,
    path)`, which the service checks, so it is good for this service and request only.
    """

    def __init__(
        self,
        audience: str,
        munge: str = "munge",
        *,
        run: _Run = _run,
        timeout_s: float = 5.0,
    ) -> None:
        self.audience = audience
        self._munge = munge
        self._run = run
        self._timeout_s = timeout_s

    def headers(self, method: str, path: str) -> dict[str, str]:
        argv = [self._munge, "--string", munge_payload(self.audience, method, path)]
        try:
            done = self._run(argv, timeout=self._timeout_s)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MetadataError(
                f"cannot create a MUNGE credential with {self._munge!r}: {exc}. Install MUNGE "
                "and start munged, or set [metadata].token_file for a bearer token"
            ) from exc
        credential = done.stdout.strip()
        if done.returncode != 0 or not credential:
            raise MetadataError(
                f"{self._munge!r} failed (exit {done.returncode}): {done.stderr.strip()}. "
                "Check that munged is running on this host"
            )
        return {MUNGE_HEADER: credential}


class HttpMetadataConfig(BaseModel):
    """The `[metadata]` section of ebs.toml when `url` is the metadata service (P1-06).

    `url` is the service root (`https://ebs-meta.example`); routes live under `/api/v1`.
    `auth` defaults to `token` when a token is configured (`token_file` or `$EBS_META_TOKEN`),
    else `munge`. `audience` (MUNGE only) must equal the service's `[meta_service].audience`;
    by default it is the `host[:port]` of `url`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    url: Annotated[StrictStr, Field(min_length=1)]
    auth: Literal["munge", "token"] | None = None
    token_file: StrictStr | None = None
    ca_file: StrictStr | None = None
    munge: StrictStr = "munge"
    audience: StrictStr | None = None  # MUNGE binding; default: host[:port] of `url`
    timeout_s: Annotated[StrictInt, Field(ge=1, le=600)] = int(DEFAULT_TIMEOUT_S)
    retries: Annotated[StrictInt, Field(ge=0, le=10)] = 4

    @classmethod
    def from_mapping(cls, section: Mapping[str, object]) -> HttpMetadataConfig:
        try:
            cfg = cls.model_validate(dict(section))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '(section)'}: {e['msg']}"
                for e in exc.errors()
            )
            raise ConfigError(f"invalid [metadata] configuration: {problems}") from None
        parts = urlsplit(cfg.url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError(
                f"[metadata].url {cfg.url!r} is not an http(s) URL of the metadata service "
                "(https://ebs-meta.example)"
            )
        return cfg


def _read_token_file(path: Path) -> str:
    try:
        info = path.stat()
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError(f"[metadata].token_file {path} is readable by others; chmod 600 it")
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read [metadata].token_file {path}: {exc}") from exc


def _credentials(cfg: HttpMetadataConfig, environ: Mapping[str, str]) -> CredentialSource:
    token: str | None = None
    if cfg.token_file is not None:
        token = _read_token_file(Path(cfg.token_file).expanduser())
    elif environ.get(TOKEN_ENV):
        token = environ[TOKEN_ENV]
    auth = cfg.auth or ("token" if token is not None else "munge")
    if auth == "munge":
        audience = cfg.audience or urlsplit(cfg.url).netloc.lower()
        return MungeCredential(audience, cfg.munge)
    if token is None:
        raise ConfigError(
            f"[metadata].auth = 'token' needs a token: set [metadata].token_file or ${TOKEN_ENV}"
        )
    return BearerCredential(token)


def _q(segment: str) -> str:
    """One path segment, fully quoted; `.` and `..` are escaped too, or URL normalization would
    remove them (action ids may be dots)."""
    if segment and set(segment) == {"."}:
        return "%2E" * len(segment)
    return quote(segment, safe="")


def _chunks(items: Sequence[_T], size: int) -> Iterator[list[_T]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


class HttpMetadataStore:
    """MetadataStore over the metadata service's REST API (interfaces.md § 6)."""

    def __init__(
        self,
        url: str,
        *,
        credentials: CredentialSource | None = None,
        client: httpx.Client | None = None,
        retries: int = 4,
        backoff_s: float = 0.5,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        verify: str | bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = url.rstrip("/")
        self._credentials = credentials
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_s), verify=verify)
        self._retries = retries
        self._backoff_s = backoff_s
        self._timeout_s = timeout_s
        self._sleep = sleep

    @classmethod
    def from_config(
        cls,
        config: HttpMetadataConfig | Mapping[str, object],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> HttpMetadataStore:
        cfg = (
            config
            if isinstance(config, HttpMetadataConfig)
            else HttpMetadataConfig.from_mapping(config)
        )
        env = os.environ if environ is None else environ
        return cls(
            cfg.url,
            credentials=_credentials(cfg, env),
            retries=cfg.retries,
            timeout_s=float(cfg.timeout_s),
            verify=cfg.ca_file if cfg.ca_file is not None else True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __repr__(self) -> str:
        return f"HttpMetadataStore({self.base_url})"

    # --- transport ------------------------------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        what: str,
        *,
        body: BaseModel | None = None,
        params: Mapping[str, str] | None = None,
        idempotent: bool,
    ) -> httpx.Response:
        url = f"{self.base_url}{API_PREFIX}{path}"
        try:
            content = None if body is None else body.model_dump_json().encode()
        except PydanticSerializationError as exc:
            raise MetadataError(f"{what}: the request cannot be sent as JSON: {exc}") from None
        attempt = 0
        while True:
            headers = {"Accept": "application/json"}
            if content is not None:
                headers["Content-Type"] = "application/json"
            if self._credentials is not None:
                headers |= self._credentials.headers(method, unquote(path))
            problem: str
            wait: float | None = None  # the service's Retry-After of a 429
            try:
                response = self._client.request(
                    method,
                    url,
                    content=content,
                    params=params,
                    headers=headers,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                problem, retry = f"cannot connect: {exc}", True  # nothing was sent
            except httpx.TimeoutException as exc:
                problem, retry = f"timed out after {self._timeout_s:g} s: {exc}", idempotent
            except httpx.TransportError as exc:
                problem, retry = f"transport error: {exc}", idempotent
            else:
                if response.status_code < 400:
                    return response
                detail = self._detail(response)
                limited = response.status_code == 429
                if response.status_code not in _RETRY_STATUS and not limited:
                    raise MetadataHttpError(
                        self._client_error(what, response.status_code, detail),
                        response.status_code,
                    )
                problem = f"HTTP {response.status_code}: {detail}"
                # A rate-limited request was refused before anything ran: always retry it.
                retry = idempotent or limited
                if limited:
                    wait = _retry_after(response)
            if not retry or attempt >= self._retries:
                raise MetadataError(
                    f"{what}: metadata service at {self.base_url} failed: {problem}. Check "
                    "[metadata].url and that the service is up (GET /readyz)"
                )
            delay = min(self._backoff_s * 2**attempt, _BACKOFF_CAP_S) if wait is None else wait
            _log.warning("metadata service call failed, retrying", call=what, problem=problem)
            attempt += 1
            self._sleep(delay)

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        return str(detail) if detail else (response.reason_phrase or "no detail")

    def _client_error(self, what: str, status: int, detail: str) -> str:
        if status == 401:
            return (
                f"{what}: authentication with the metadata service at {self.base_url} failed: "
                f"{detail}. Check that munged runs on this host, or [metadata].token_file"
            )
        if status == 403:
            return f"{what}: {detail}"
        return f"{what}: {detail}" if status in (409, 422) else f"{what}: HTTP {status}: {detail}"

    def _pages(
        self,
        path: str,
        what: str,
        model: type[_P],
        params: Mapping[str, str] | None = None,
    ) -> list[_P]:
        """Every page of a list route (P2-05 R3): follow `next` until the last page."""
        pages: list[_P] = []
        cursor: str | None = None
        while True:
            query = dict(params or {}) | ({} if cursor is None else {"cursor": cursor})
            r = self._call("GET", path, what, params=query, idempotent=True)
            page = self._parse(r, model, what)
            pages.append(page)
            following = getattr(page, "next", None)
            if following is None:
                return pages
            if following == cursor:
                raise MetadataError(
                    f"{what}: the metadata service at {self.base_url} returned the same page "
                    "cursor twice; is the service the same version as this client?"
                )
            cursor = following

    def _parse(self, response: httpx.Response, model: type[_M], what: str) -> _M:
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise MetadataError(
                f"{what}: unexpected response from the metadata service at {self.base_url} "
                f"(HTTP {response.status_code}): {exc.error_count()} validation errors; is "
                "[metadata].url the service root and are client and service the same version?"
            ) from None

    # --- action cache ---------------------------------------------------------------------------

    def _cache_path(self, domain: str, key: Digest, what: str) -> str:
        check_domain(domain)
        check_digest(key, f"{what} key")
        return f"/domains/{_q(domain)}/cache/{_q(str(key))}"

    def cache_get(self, domain: str, key: Digest) -> ResultManifest | None:
        what = f"cache_get {domain}/{key}"
        path = self._cache_path(domain, key, "cache_get")
        r = self._call("GET", path, what, idempotent=True)
        return self._parse(r, OptionalResult, what).result

    def cache_get_many(self, domain: str, keys: Iterable[Digest]) -> dict[Digest, ResultManifest]:
        """`cache_get` for many keys in batches of `BATCH_MAX`; only hits are returned."""
        check_domain(domain)
        wanted = list(dict.fromkeys(check_digest(k, "cache_get_many key") for k in keys))
        hits: dict[Digest, ResultManifest] = {}
        for chunk in _chunks(wanted, BATCH_MAX):
            what = f"cache_get_many {domain} ({len(chunk)} keys)"
            body = DigestBatch.model_validate({"digests": chunk})
            r = self._call(
                "POST", f"/domains/{_q(domain)}/cache/lookup", what, body=body, idempotent=True
            )
            for key, result in self._parse(r, CacheHits, what).results.items():
                hits[Digest.parse(key)] = result
        return hits

    def cache_put(self, domain: str, key: Digest, result: ResultManifest) -> bool:
        what = f"cache_put {domain}/{key}"
        path = self._cache_path(domain, key, "cache_put")
        checked = revalidate(result, ResultManifest, what)
        r = self._call("PUT", path, what, body=checked, idempotent=True)
        return self._parse(r, Changed, what).changed

    def cache_replace_failed(self, domain: str, key: Digest, result: ResultManifest) -> bool:
        what = f"cache_replace_failed {domain}/{key}"
        path = self._cache_path(domain, key, "cache_replace_failed") + "/replace-failed"
        checked = revalidate(result, ResultManifest, what)
        r = self._call("POST", path, what, body=checked, idempotent=False)
        return self._parse(r, Changed, what).changed

    # --- builds ---------------------------------------------------------------------------------

    def create_build(self, b: BuildCreate) -> BuildId:
        spec = revalidate(b, BuildCreate, "create_build")
        r = self._call("POST", "/builds", "create_build", body=spec, idempotent=False)
        return BuildId(self._parse(r, CreatedBuild, "create_build").id)

    def add_actions(self, build: BuildId, actions: Sequence[ActionRow]) -> None:
        what = f"add_actions to build {build}"
        rows = [revalidate(a, ActionRow, what) for a in actions]
        seen: set[str] = set()
        for row in rows:
            if row.action_id in seen:
                raise MetadataError(f"add_actions: action id {row.action_id!r} appears twice")
            seen.add(row.action_id)
        batches = list(_chunks(rows, BATCH_MAX)) or [[]]  # an empty call still checks the build
        for chunk in batches:
            body = ActionBatch.model_validate({"actions": chunk})
            self._call("POST", f"/builds/{int(build)}/actions", what, body=body, idempotent=False)

    def set_action_state(
        self, build: BuildId, action_id: str, state: ActionState, **fields: object
    ) -> None:
        what = f"set_action_state {build}/{action_id}"
        wire: dict[str, object] = {
            name: str(value) if isinstance(value, Digest) else value
            for name, value in fields.items()
        }
        try:
            body = StateChange.model_validate({"state": state, "fields": wire})
        except ValidationError as exc:
            raise MetadataError(f"{what}: invalid request: {exc}") from None
        self._call(
            "POST",
            f"/builds/{int(build)}/actions/{_q(action_id)}/state",
            what,
            body=body,
            idempotent=False,
        )

    def record_result(self, build: BuildId, action_id: str, result: ResultManifest) -> None:
        what = f"record_result {build}/{action_id}"
        checked = revalidate(result, ResultManifest, what)
        self._call(
            "PUT",
            f"/builds/{int(build)}/actions/{_q(action_id)}/result",
            what,
            body=checked,
            idempotent=True,
        )

    def get_result(self, build: BuildId, action_id: str) -> ResultManifest | None:
        what = f"get_result {build}/{action_id}"
        r = self._call(
            "GET", f"/builds/{int(build)}/actions/{_q(action_id)}/result", what, idempotent=True
        )
        return self._parse(r, OptionalResult, what).result

    def finish_build(
        self, build: BuildId, status: BuildStatus, *, final_plan: Digest | None = None
    ) -> None:
        what = f"finish_build {build}"
        try:
            body = BuildFinish.model_validate({"status": status, "final_plan": final_plan})
        except ValidationError as exc:
            raise MetadataError(f"{what}: invalid status {status!r}: {exc}") from None
        self._call("POST", f"/builds/{int(build)}/finish", what, body=body, idempotent=False)

    def record_release_imports(self, build: BuildId, imports: Sequence[ReleaseImport]) -> None:
        what = f"record_release_imports {build}"
        try:
            body = ReleaseImportBatch(imports=list(imports))
        except ValidationError as exc:
            raise MetadataError(f"{what}: invalid request: {exc}") from None
        # Recording the same rows again is a no-op in the store, so a retry is safe.
        self._call(
            "POST", f"/builds/{int(build)}/release-imports", what, body=body, idempotent=True
        )

    def list_release_imports(self, build: BuildId) -> list[ReleaseImport]:
        what = f"list_release_imports {build}"
        pages = self._pages(f"/builds/{int(build)}/release-imports", what, ReleaseImportList)
        return [x for page in pages for x in page.imports]

    def get_build(self, build: BuildId) -> BuildView:
        what = f"get_build {build}"
        r = self._call("GET", f"/builds/{int(build)}", what, idempotent=True)
        return self._parse(r, BuildView, what)

    def list_actions(self, build: BuildId, *, state: ActionState | None = None) -> list[ActionRow]:
        what = f"list_actions {build}"
        params = None if state is None else {"state": state}
        pages = self._pages(f"/builds/{int(build)}/actions", what, ActionList, params)
        return [a for page in pages for a in page.actions]

    def resolve_output(self, domain: str, object_id: Digest) -> Digest | None:
        check_domain(domain)
        check_digest(object_id, "resolve_output object_id")
        what = f"resolve_output {domain}/{object_id}"
        r = self._call(
            "GET", f"/domains/{_q(domain)}/outputs/{_q(str(object_id))}", what, idempotent=True
        )
        return self._parse(r, OptionalDigest, what).digest

    # --- toolchain registry (P1-07) -------------------------------------------------------------

    def register_toolchain(self, spec: ToolchainCreate, manifest: bytes) -> RegisterOutcome:
        checked = check_toolchain(spec, manifest)
        what = f"register_toolchain {checked.module!r}"
        body = ToolchainRegistration(
            spec=checked, manifest_b64=base64.b64encode(manifest).decode("ascii")
        )
        # Idempotent by design: registering an id again changes nothing new (P1-07).
        r = self._call("POST", "/toolchains", what, body=body, idempotent=True)
        return self._parse(r, RegisterOutcome, what)

    def _toolchains(
        self, path: str, what: str, params: Mapping[str, str] | None = None
    ) -> list[ToolchainRecord]:
        return [
            t for page in self._pages(path, what, ToolchainList, params) for t in page.toolchains
        ]

    def get_toolchain(self, toolchain_id: Digest) -> ToolchainRecord | None:
        check_digest(toolchain_id, "get_toolchain id")
        found = self._toolchains(
            f"/toolchains/{_q(str(toolchain_id))}", f"get_toolchain {toolchain_id}"
        )
        return found[0] if found else None

    def current_toolchain(self, module: str) -> ToolchainRecord | None:
        found = self._toolchains(
            "/toolchains/current", f"current_toolchain {module!r}", {"module": module}
        )
        return found[0] if found else None

    def list_toolchains(
        self, *, module: str | None = None, current_only: bool = False
    ) -> list[ToolchainRecord]:
        params = {"current_only": "true" if current_only else "false"}
        if module is not None:
            params["module"] = module
        return self._toolchains("/toolchains", "list_toolchains", params)

    def toolchain_manifest(self, toolchain_id: Digest) -> bytes | None:
        check_digest(toolchain_id, "toolchain_manifest id")
        what = f"toolchain_manifest {toolchain_id}"
        r = self._call(
            "GET", f"/toolchains/{_q(str(toolchain_id))}/manifest", what, idempotent=True
        )
        encoded = self._parse(r, ToolchainManifest, what).manifest_b64
        if encoded is None:
            return None
        try:
            return base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise MetadataError(f"{what}: the service sent an invalid manifest: {exc}") from None

    def touch_toolchain(self, toolchain_id: Digest) -> None:
        check_digest(toolchain_id, "touch_toolchain id")
        self._call(
            "POST",
            f"/toolchains/{_q(str(toolchain_id))}/touch",
            f"touch_toolchain {toolchain_id}",
            idempotent=True,
        )

    # --- access tracking, events ----------------------------------------------------------------

    def touch(self, domain: str, digests: Iterable[Digest]) -> None:
        check_domain(domain)
        keys = sorted({check_digest(d, "touch digest") for d in digests}, key=str)
        for chunk in _chunks(keys, BATCH_MAX):
            body = DigestBatch.model_validate({"digests": chunk})
            self._call(
                "POST", f"/domains/{_q(domain)}/touch", f"touch {domain}", body=body,
                idempotent=True,
            )  # fmt: skip

    def register_blobs(self, domain: str, blobs: Iterable[BlobRef]) -> None:
        check_domain(domain)
        refs = check_blobs(blobs)
        for chunk in _chunks(refs, BATCH_MAX):
            self._call(
                "POST",
                f"/domains/{_q(domain)}/blobs",
                f"register_blobs {domain}",
                body=BlobBatch(blobs=chunk),
                idempotent=True,
            )

    # --- leases and pins ------------------------------------------------------------------------

    def acquire_lease(
        self, domain: str, build: BuildId, digests: Iterable[Digest], ttl_s: int
    ) -> LeaseId:
        check_domain(domain)
        check_lease_ttl(ttl_s)
        keys = sorted({check_digest(d, "acquire_lease digest") for d in digests}, key=str)
        first, rest = keys[:BATCH_MAX], keys[BATCH_MAX:]
        body = LeaseRequest(domain=domain, digests=first, ttl_s=ttl_s)
        what = f"acquire_lease for build {build}"
        r = self._call("POST", f"/builds/{int(build)}/leases", what, body=body, idempotent=False)
        lease = LeaseId(self._parse(r, CreatedLease, what).id)
        if rest:  # larger leases grow by renewals, each at most one batch
            self.renew_lease(build, lease, ttl_s, add=rest)
        return lease

    def renew_lease(
        self, build: BuildId, lease: LeaseId, ttl_s: int, *, add: Iterable[Digest] = ()
    ) -> None:
        check_lease_ttl(ttl_s)
        keys = sorted({check_digest(d, "renew_lease digest") for d in add}, key=str)
        for chunk in _chunks(keys, BATCH_MAX) if keys else iter([[]]):
            self._call(
                "POST",
                f"/builds/{int(build)}/leases/{int(lease)}/renew",
                f"renew_lease {lease}",
                body=LeaseRequest(digests=chunk, ttl_s=ttl_s),
                idempotent=True,
            )

    # --- driver lease (P2-08) -------------------------------------------------------------------

    def claim_driver(self, build: BuildId, owner: str, ttl_s: int) -> DriverLease:
        check_driver_owner(owner, "claim_driver")
        check_lease_ttl(ttl_s)
        what = f"claim_driver for build {build}"
        body = DriverClaim(owner=owner, ttl_s=ttl_s)
        # A retried claim by the same owner is a renewal: safe to repeat.
        r = self._call("POST", f"/builds/{int(build)}/driver", what, body=body, idempotent=True)
        return self._parse(r, DriverLease, what)

    def release_driver(self, build: BuildId, owner: str) -> None:
        check_driver_owner(owner, "release_driver")
        self._call(
            "POST",
            f"/builds/{int(build)}/driver/release",
            f"release_driver for build {build}",
            body=DriverRelease(owner=owner),
            idempotent=True,
        )

    def pin_build(self, build: BuildId, *, by: str, reason: str = "") -> None:
        # The service records the authenticated caller as the pin's author, not `by`.
        _, reason = check_pin(by, reason)
        what = f"pin_build {build}"
        body = PinRequest(reason=reason)
        self._call("PUT", f"/builds/{int(build)}/pin", what, body=body, idempotent=True)

    def unpin_build(self, build: BuildId) -> bool:
        what = f"unpin_build {build}"
        r = self._call("DELETE", f"/builds/{int(build)}/pin", what, idempotent=True)
        return self._parse(r, Changed, what).changed

    # --- events ---------------------------------------------------------------------------------

    def emit(self, event: Event) -> None:
        checked = revalidate(event, Event, "emit")
        self._call(
            "POST",
            f"/builds/{int(checked.build)}/events",
            f"emit {checked.type}",
            body=checked,
            idempotent=False,
        )

    # --- releases and channels (P2-02) -----------------------------------------------------------

    def create_release(self, spec: ReleaseCreate, *, by: str) -> ReleaseView:
        # The service records the authenticated caller as the author, not `by`.
        checked = revalidate(spec, ReleaseCreate, "create_release")
        what = f"create_release {checked.name} {checked.version}"
        r = self._call("POST", "/releases", what, body=checked, idempotent=False)
        return self._parse(r, ReleaseView, what)

    def _releases(self, path: str, what: str, params: Mapping[str, str]) -> list[ReleaseView]:
        return [r for page in self._pages(path, what, ReleaseList, params) for r in page.releases]

    def get_release(self, domain: str, name: str, version: str) -> ReleaseView | None:
        check_domain(domain)
        params = {"domain": domain, "name": name, "version": version}
        found = self._releases("/releases", f"get_release {name} {version}", params)
        return found[0] if found else None

    def get_release_by_id(self, release: ReleaseId) -> ReleaseView | None:
        found = self._releases(f"/releases/{int(release)}", f"get_release {release}", {})
        return found[0] if found else None

    def list_releases(self, domain: str, *, name: str | None = None) -> list[ReleaseView]:
        check_domain(domain)
        params = {"domain": domain} | ({} if name is None else {"name": name})
        return self._releases("/releases", f"list_releases {domain}", params)

    def yank_release(self, release: ReleaseId, *, by: str, reason: str) -> ReleaseView:
        what = f"yank_release {release}"
        try:
            req = YankRequest(reason=reason)
        except ValidationError:
            raise MetadataError(f"{what}: give a reason (1-4096 characters)") from None
        r = self._call("POST", f"/releases/{int(release)}/yank", what, body=req, idempotent=True)
        return self._parse(r, ReleaseView, what)

    def set_channel(
        self,
        domain: str,
        name: str,
        channel: str,
        release: ReleaseId,
        *,
        by: str,
        gate: GateCheck | None = None,
    ) -> ChannelMove:
        """The service checks its own gate policy; `gate` only names the gate build to check."""
        what = f"set_channel {name}:{channel}"
        try:
            req = ChannelSet(
                domain=domain, name=name, channel=channel, release=int(release),
                gate_build=None if gate is None else int(gate.build),
            )  # fmt: skip
        except ValidationError as exc:
            raise MetadataError(f"{what}: invalid request: {exc}") from None
        r = self._call("PUT", "/channels", what, body=req, idempotent=False)
        return self._parse(r, ChannelMove, what)

    def get_channel(self, domain: str, name: str, channel: str) -> ChannelView | None:
        check_domain(domain)
        what = f"get_channel {name}:{channel}"
        params = {"domain": domain, "name": name, "channel": channel}
        r = self._call("GET", "/channels", what, params=params, idempotent=True)
        found = self._parse(r, ChannelList, what).channels
        return found[0] if found else None

    def channel_history(self, domain: str, name: str, channel: str) -> list[ChannelMove]:
        check_domain(domain)
        what = f"channel_history {name}:{channel}"
        params = {"domain": domain, "name": name, "channel": channel}
        pages = self._pages("/channels/history", what, ChannelHistory, params)
        return [m for page in pages for m in page.moves]

    def gate_builds(self, domain: str, flow: str, ids: Iterable[Digest]) -> list[BuildId]:
        check_domain(domain)
        keys = sorted({check_digest(d, "gate_builds id") for d in ids}, key=str)
        what = f"gate_builds {domain}/{flow}"
        found: set[int] = set()
        for chunk in _chunks(keys, BATCH_MAX):
            r = self._call(
                "POST", f"/domains/{_q(domain)}/gate-builds", what,
                body=GateQuery(flow=flow, ids=chunk), idempotent=True,
            )  # fmt: skip
            found.update(self._parse(r, BuildList, what).builds)
        return [BuildId(b) for b in sorted(found, reverse=True)]

    def record_export(self, release: ReleaseId, to_domain: str, *, by: str) -> ReleaseView:
        check_domain(to_domain)
        what = f"record_export {release} to {to_domain}"
        r = self._call(
            "POST", f"/releases/{int(release)}/exports", what,
            body=ExportRequest(to_domain=to_domain), idempotent=False,
        )  # fmt: skip
        return self._parse(r, ReleaseView, what)

    # --- provenance (P2-04) ----------------------------------------------------------------------

    def provenance_upstream(
        self, domain: str, *, ids: Iterable[Digest], keys: Iterable[Digest], depth: int
    ) -> ProvenanceGraph:
        return self._walk("upstream", domain, ids, keys, depth)

    def provenance_downstream(
        self, domain: str, *, ids: Iterable[Digest], keys: Iterable[Digest], depth: int
    ) -> ProvenanceGraph:
        return self._walk("downstream", domain, ids, keys, depth)

    def _walk(
        self, way: str, domain: str, ids: Iterable[Digest], keys: Iterable[Digest], depth: int
    ) -> ProvenanceGraph:
        domain, id_list, key_list, levels = check_walk(
            domain, ids, keys, depth, f"provenance_{way}"
        )
        what = f"provenance_{way} {domain}"
        if len(id_list) + len(key_list) > PROVENANCE_START_MAX:
            raise MetadataError(
                f"{what}: a walk starts from at most {PROVENANCE_START_MAX} ids and keys, got "
                f"{len(id_list) + len(key_list)}; query fewer outputs at a time"
            )
        query = ProvenanceQuery.model_validate({"ids": id_list, "keys": key_list, "depth": levels})
        r = self._call(
            "POST", f"/domains/{_q(domain)}/provenance/{way}", what, body=query, idempotent=True
        )
        return self._parse(r, ProvenanceGraph, what)

    def list_exports(self, release: ReleaseId) -> list[ExportRecord]:
        what = f"list_exports {release}"
        pages = self._pages(f"/releases/{int(release)}/exports", what, ExportList)
        return [x for page in pages for x in page.exports]
