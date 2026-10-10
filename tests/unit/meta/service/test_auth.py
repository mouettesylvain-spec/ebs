"""Authentication of the metadata service (P1-06 R3, decision D1)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import timedelta

import pytest

from ebs.core.clock import FakeClock
from ebs.meta.api import API_PREFIX
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.auth import (
    MUNGE_HEADER,
    AuthenticationError,
    MungeIdentity,
    Principal,
    UnmungeVerifier,
)
from ebs.meta.service.tokens import InMemoryTokenStore, hash_token
from tests.helpers.meta_http import FakeMunge, Service, make_service

WHOAMI = f"{API_PREFIX}/whoami"


@pytest.fixture
def svc() -> Service:
    return make_service(InMemoryMetadataStore(clock=FakeClock()))


def _whoami(svc: Service, headers: dict[str, str]) -> tuple[int, dict[str, object]]:
    r = svc.client.get(WHOAMI, headers=headers)
    return r.status_code, r.json()


# R3
def test_munge_ok(svc: Service) -> None:
    status, body = _whoami(svc, FakeMunge(uid=1001).headers())
    assert status == 200
    assert body == {"name": "bob", "via": "munge", "token_kind": None, "team": None}


# R3
@pytest.mark.parametrize("case", ["replayed", "expired", "garbage", "unknown uid"])
def test_munge_replay_or_expired_rejected(svc: Service, case: str) -> None:
    good = FakeMunge().headers()
    assert _whoami(svc, good)[0] == 200
    headers = {
        "replayed": good,  # the same credential a second time
        "expired": {MUNGE_HEADER: "expired:1000:1"},
        "garbage": {MUNGE_HEADER: "not-a-credential"},
        "unknown uid": {MUNGE_HEADER: "cred:4242:1"},
    }[case]
    status, body = _whoami(svc, headers)
    assert status == 401
    assert "MUNGE" in str(body["detail"]) or "uid" in str(body["detail"])


# R3
def test_bearer(svc: Service) -> None:
    token = svc.tokens.add("ci-cpu", kind="ci", team="rv32x-cpu")
    status, body = _whoami(svc, {"Authorization": f"Bearer {token}"})
    assert status == 200
    assert body == {"name": "ci-cpu", "via": "token", "token_kind": "ci", "team": "rv32x-cpu"}
    # Only the hash is stored, never the token itself.
    assert token not in repr(svc.tokens._by_hash)
    assert hash_token(token) in svc.tokens._by_hash


# R3
def test_bearer_revoked_or_expired(svc: Service) -> None:
    clock = FakeClock()
    tokens = InMemoryTokenStore(clock=clock)
    svc = make_service(InMemoryMetadataStore(clock=clock), tokens=tokens)
    revoked = tokens.add("ci-a", kind="ci", team="t")
    tokens.revoke("ci-a")
    expiring = tokens.add("ci-b", kind="ci", team="t", expires_at=clock.now() + timedelta(hours=1))
    assert _whoami(svc, {"Authorization": f"Bearer {expiring}"})[0] == 200
    clock.advance(3601)
    for token in (revoked, expiring):
        status, body = _whoami(svc, {"Authorization": f"Bearer {token}"})
        assert status == 401, body


# R3
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic YWxpY2U6cHc="},
        {"Authorization": "Bearer"},
    ],
    ids=["none", "unknown-token", "basic", "empty-bearer"],
)
def test_401(svc: Service, headers: dict[str, str]) -> None:
    status, body = _whoami(svc, headers)
    assert status == 401
    assert isinstance(body["detail"], str)
    assert body["detail"]
    # Every API route is protected, not just whoami.
    r = svc.client.get(f"{API_PREFIX}/builds/1", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"]
    # Bodies are not parsed before authentication: 401, not 422, for an invalid body.
    for method, path in (("POST", "/builds"), ("PUT", "/domains/test/cache/x")):
        r = svc.client.request(method, API_PREFIX + path, content=b"{}", headers=headers)
        assert r.status_code == 401, (method, path, r.text)


# R3: health probes stay open (load balancers carry no credentials).
def test_probes_need_no_credentials(svc: Service) -> None:
    assert svc.client.get("/healthz").status_code == 200
    assert svc.client.get("/readyz").status_code == 200


# --- the real verifier, with `unmunge` faked at the process boundary ------------------------------

_DECODED = """\
STATUS:          Success (0)
ENCODE_HOST:     node-a.example.invalid (192.0.2.10)
ENCODE_TIME:     2026-10-10 10:00:00 +0000 (1791626400)
DECODE_TIME:     2026-10-10 10:00:01 +0000 (1791626401)
TTL:             300
CIPHER:          aes128 (4)
MAC:             sha256 (5)
ZIP:             none (0)
UID:             1000
GID:             1000
LENGTH:          0

"""


class FakeRun:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.result = (stdout, returncode, stderr)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(
        self, argv: Sequence[str], *, input: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), input))
        out, code, err = self.result
        return subprocess.CompletedProcess(list(argv), code, out, err)


# R3
def test_unmunge_success_parses_identity() -> None:
    run = FakeRun(_DECODED)
    identity = UnmungeVerifier(run=run).verify("MUNGE:AwQ...:")
    assert identity == MungeIdentity(uid=1000, gid=1000)
    argv, stdin = run.calls[0]
    assert argv[0] == "unmunge"
    assert {"--numeric", "--metadata=-", "--output=/dev/null"} <= set(argv)
    assert stdin == "MUNGE:AwQ...:"  # the credential goes on stdin, never on the command line


# R3
@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("Replayed credential (17)", 17),
        ("Expired credential (15)", 15),
        ("Rewound credential (16)", 16),
    ],
)
def test_unmunge_refusals(status: str, code: int) -> None:
    out = _DECODED.replace("Success (0)", status)
    with pytest.raises(AuthenticationError, match=status.split(" (")[0]):
        UnmungeVerifier(run=FakeRun(out, returncode=code)).verify("MUNGE:x:")


def test_unmunge_failures_are_auth_errors() -> None:
    with pytest.raises(AuthenticationError, match="unmunge"):
        UnmungeVerifier(run=FakeRun("", 1, "unmunge: Error: Invalid credential")).verify("x")
    with pytest.raises(AuthenticationError, match="UID"):
        UnmungeVerifier(run=FakeRun("STATUS:  Success (0)\nLENGTH:  0\n")).verify("x")

    def missing(
        argv: Sequence[str], *, input: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    with pytest.raises(AuthenticationError, match="unmunge"):
        UnmungeVerifier(run=missing).verify("x")


def test_principal_is_hashable_value() -> None:
    assert Principal("alice", via="munge") == Principal("alice", via="munge")


# R3: tokens are created once (printed) and revoked through the admin entry point.
def test_token_admin_commands() -> None:
    import io

    from ebs.meta.service.__main__ import main

    clock = FakeClock()
    store = InMemoryTokenStore(clock=clock)
    out = io.StringIO()
    argv = ["token", "create", "ci-cpu", "--kind", "ci", "--team", "rv32x", "--expires-days", "1"]
    assert main(argv, tokens=lambda env: store, clock=clock, out=out) == 0
    token = out.getvalue().strip()
    info = store.lookup(hash_token(token))
    assert info is not None
    assert (info.principal, info.kind, info.team) == ("ci-cpu", "ci", "rv32x")
    assert info.expires_at == clock.now() + timedelta(days=1)
    assert main(["token", "create", "ci-x", "--kind", "ci"], tokens=lambda env: store) == 2
    assert main(["token", "revoke", "ci-cpu"], tokens=lambda env: store, out=out) == 0
    assert "revoked 1 token(s) of ci-cpu" in out.getvalue()
    assert store.lookup(hash_token(token)) is None


# R3 (review blocker): a payload imitating metadata lines must not change the identity.
@pytest.mark.parametrize(
    "stdout",
    [
        _DECODED + "UID:             0\nGID:             0\n",  # payload after the blank line
        _DECODED.replace("LENGTH:          0", "LENGTH:          32"),  # non-empty payload
        _DECODED.replace("LENGTH:          0\n", "LENGTH:          0\nUID:             0\n"),
    ],
    ids=["payload-after-block", "non-empty-payload", "duplicate-uid"],
)
def test_unmunge_payload_cannot_forge_identity(stdout: str) -> None:
    verifier = UnmungeVerifier(run=FakeRun(stdout))
    if stdout.startswith(_DECODED):
        # unmunge's own lines win; whatever follows the metadata block is ignored.
        assert verifier.verify("MUNGE:x:") == MungeIdentity(uid=1000, gid=1000)
    else:
        with pytest.raises(AuthenticationError, match=r"payload|twice"):
            verifier.verify("MUNGE:x:")
