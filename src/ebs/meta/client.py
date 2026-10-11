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

import os
import stat
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol, TypeVar
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from pydantic_core import PydanticSerializationError

from ebs.core.digest import Digest
from ebs.core.errors import ConfigError, MetadataError
from ebs.core.log import get_logger
from ebs.meta._common import check_digest, revalidate
from ebs.meta.api import (
    API_PREFIX,
    BATCH_MAX,
    ActionBatch,
    ActionList,
    ActionRow,
    ActionState,
    BuildCreate,
    BuildFinish,
    BuildId,
    BuildStatus,
    BuildView,
    CacheHits,
    Changed,
    CreatedBuild,
    DigestBatch,
    Event,
    OptionalDigest,
    OptionalResult,
    ResultManifest,
    StateChange,
    check_domain,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MUNGE_HEADER",
    "TOKEN_ENV",
    "BearerCredential",
    "CredentialSource",
    "HttpMetadataConfig",
    "HttpMetadataStore",
    "MungeCredential",
]

_log = get_logger(__name__)

DEFAULT_TIMEOUT_S: Final = 10.0
MUNGE_HEADER: Final = "X-EBS-Munge"
TOKEN_ENV: Final = "EBS_META_TOKEN"
"""Environment variable holding a bearer token (CI jobs); `[metadata].token_file` is the other
way to give one."""

_RETRY_STATUS: Final = frozenset({500, 502, 503, 504})
_BACKOFF_CAP_S: Final = 8.0

_M = TypeVar("_M", bound=BaseModel)
_T = TypeVar("_T")


class CredentialSource(Protocol):
    """Headers that authenticate one request; called again for every attempt."""

    def headers(self) -> dict[str, str]: ...


class BearerCredential:
    """`Authorization: Bearer <token>` for CI and services (D1)."""

    def __init__(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise ConfigError("the metadata service token is empty")
        self._token = token

    def headers(self) -> dict[str, str]:
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
    """A fresh MUNGE credential per request (`munge --no-input`), sent as `X-EBS-Munge` (D1).

    MUNGE proves the caller's uid to the service; munged on the login and compute nodes must
    share the service host's key.
    """

    def __init__(self, munge: str = "munge", *, run: _Run = _run, timeout_s: float = 5.0) -> None:
        self._argv = [munge, "--no-input"]
        self._run = run
        self._timeout_s = timeout_s

    def headers(self) -> dict[str, str]:
        try:
            done = self._run(self._argv, timeout=self._timeout_s)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MetadataError(
                f"cannot create a MUNGE credential with {self._argv[0]!r}: {exc}. Install MUNGE "
                "and start munged, or set [metadata].token_file for a bearer token"
            ) from exc
        credential = done.stdout.strip()
        if done.returncode != 0 or not credential:
            raise MetadataError(
                f"{self._argv[0]!r} failed (exit {done.returncode}): {done.stderr.strip()}. "
                "Check that munged is running on this host"
            )
        return {MUNGE_HEADER: credential}


class HttpMetadataConfig(BaseModel):
    """The `[metadata]` section of ebs.toml when `url` is the metadata service (P1-06).

    `url` is the service root (`https://ebs-meta.example`); routes live under `/api/v1`.
    `auth` defaults to `token` when a token is configured (`token_file` or `$EBS_META_TOKEN`),
    else `munge`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    url: Annotated[StrictStr, Field(min_length=1)]
    auth: Literal["munge", "token"] | None = None
    token_file: StrictStr | None = None
    ca_file: StrictStr | None = None
    munge: StrictStr = "munge"
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
        return MungeCredential(cfg.munge)
    if token is None:
        raise ConfigError(
            f"[metadata].auth = 'token' needs a token: set [metadata].token_file or ${TOKEN_ENV}"
        )
    return BearerCredential(token)


def _q(segment: str) -> str:
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
                headers |= self._credentials.headers()
            problem: str
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
                if response.status_code not in _RETRY_STATUS:
                    raise MetadataError(self._client_error(what, response.status_code, detail))
                problem = f"HTTP {response.status_code}: {detail}"
                retry = idempotent
            if not retry or attempt >= self._retries:
                raise MetadataError(
                    f"{what}: metadata service at {self.base_url} failed: {problem}. Check "
                    "[metadata].url and that the service is up (GET /readyz)"
                )
            delay = min(self._backoff_s * 2**attempt, _BACKOFF_CAP_S)
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

    def finish_build(self, build: BuildId, status: BuildStatus) -> None:
        what = f"finish_build {build}"
        try:
            body = BuildFinish.model_validate({"status": status})
        except ValidationError as exc:
            raise MetadataError(f"{what}: invalid status {status!r}: {exc}") from None
        self._call("POST", f"/builds/{int(build)}/finish", what, body=body, idempotent=False)

    def get_build(self, build: BuildId) -> BuildView:
        what = f"get_build {build}"
        r = self._call("GET", f"/builds/{int(build)}", what, idempotent=True)
        return self._parse(r, BuildView, what)

    def list_actions(self, build: BuildId, *, state: ActionState | None = None) -> list[ActionRow]:
        what = f"list_actions {build}"
        params = None if state is None else {"state": state}
        r = self._call("GET", f"/builds/{int(build)}/actions", what, params=params, idempotent=True)
        return self._parse(r, ActionList, what).actions

    def resolve_output(self, domain: str, object_id: Digest) -> Digest | None:
        check_domain(domain)
        check_digest(object_id, "resolve_output object_id")
        what = f"resolve_output {domain}/{object_id}"
        r = self._call(
            "GET", f"/domains/{_q(domain)}/outputs/{_q(str(object_id))}", what, idempotent=True
        )
        return self._parse(r, OptionalDigest, what).digest

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

    def emit(self, event: Event) -> None:
        checked = revalidate(event, Event, "emit")
        self._call(
            "POST",
            f"/builds/{int(checked.build)}/events",
            f"emit {checked.type}",
            body=checked,
            idempotent=False,
        )
