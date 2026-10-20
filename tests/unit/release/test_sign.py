"""DSSE envelopes signed with `ssh-keygen -Y sign`, verified offline (P3-03 R2-R4)."""

from __future__ import annotations

import base64
import copy
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ebs.cas.fs import FsCAS
from ebs.core.canon import canonical_json
from ebs.core.clock import FakeClock
from ebs.core.digest import hash_file
from ebs.core.errors import ConfigError, ReleaseError
from ebs.core.tree import build_tree
from ebs.release.attest import import_deps, provenance_statement
from ebs.release.sign import (
    NAMESPACE,
    PAYLOAD_TYPE,
    Envelope,
    Signature,
    SigningSettings,
    SshKeygen,
    SshSigner,
    check_subjects,
    pae,
    run_ssh_keygen,
    sign_statement,
    verify_envelope,
    verify_offline,
)
from tests.helpers.attest import Released, TeamKey, released, ssh_keygen_or_skip, team_key


@dataclass
class Signed:
    rel: Released
    key: TeamKey
    statement: dict[str, Any]
    envelope: Envelope
    keygen: SshKeygen


def _signed(base: Path) -> Signed:
    program = ssh_keygen_or_skip()
    rel = released(FsCAS(base / "cas", "test", clock=FakeClock()), base / "work")
    key = team_key(base / "keys")
    statement = provenance_statement(
        rel.release, rel.build, rel.plan, import_deps(rel.store, rel.build.id)
    )
    keygen = SshKeygen(program)
    return Signed(
        rel, key, statement, sign_statement(statement, SshSigner(keygen, key.key)), keygen
    )


@pytest.fixture
def signed(tmp_path: Path) -> Signed:
    return _signed(tmp_path)


@pytest.fixture(scope="module")
def signed_once(tmp_path_factory: pytest.TempPathFactory) -> Signed:
    return _signed(tmp_path_factory.mktemp("signed"))


def test_pae() -> None:
    # DSSE v1 pre-authentication encoding: "DSSEv1" SP len(type) SP type SP len(body) SP body.
    assert pae("app/x", b"hi") == b"DSSEv1 5 app/x 2 hi"
    assert pae(PAYLOAD_TYPE, b"") == b"DSSEv1 28 application/vnd.in-toto+json 0 "


# R2: the team key from config signs a DSSE envelope; the allowed-signers file verifies it.
def test_sign_verify_roundtrip(tmp_path: Path) -> None:
    program = ssh_keygen_or_skip()
    rel = released(FsCAS(tmp_path / "cas", "test", clock=FakeClock()), tmp_path / "work")
    key = team_key(tmp_path / "keys")
    statement = provenance_statement(rel.release, rel.build, rel.plan, [])
    conf = SigningSettings.from_section(
        {
            "signing": {
                "key": str(key.key),
                "allowed_signers": str(key.allowed_signers),
                "ssh_keygen": program,
            }
        }
    )
    assert conf.key == key.key
    calls: list[list[str]] = []

    def recording(argv: Sequence[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return run_ssh_keygen(argv, stdin)

    keygen = SshKeygen(conf.ssh_keygen, recording)
    assert conf.key is not None
    envelope = sign_statement(statement, SshSigner(keygen, conf.key))
    assert [program, "-Y", "sign", "-q", "-f", str(key.key), "-n", NAMESPACE] in calls
    assert NAMESPACE == "ebs-release"

    doc = json.loads(envelope.to_json())
    assert doc["payloadType"] == PAYLOAD_TYPE == "application/vnd.in-toto+json"
    assert base64.b64decode(doc["payload"]) == canonical_json(statement)
    [sig] = doc["signatures"]
    assert base64.b64decode(sig["sig"]).startswith(b"-----BEGIN SSH SIGNATURE-----")
    assert sig["keyid"].startswith("SHA256:")

    assert conf.allowed_signers is not None
    verified = verify_envelope(Envelope.from_json(envelope.to_json()), keygen, conf.allowed_signers)
    assert verified.statement == statement
    assert verified.principals == (key.principal,)
    assert any("-I" in c and key.principal in c for c in calls if "verify" in c)


# R2: a key the allowed-signers file does not trust, or another namespace, fails.
def test_untrusted_signer_or_namespace(signed: Signed, tmp_path: Path) -> None:
    other = team_key(tmp_path / "other", principal="intruder@example.invalid")
    forged = sign_statement(signed.statement, SshSigner(signed.keygen, other.key))
    with pytest.raises(ReleaseError, match=r"no signature .* allowed signer"):
        verify_envelope(forged, signed.keygen, signed.key.allowed_signers)
    # Same trusted key, signing the same bytes for another purpose (namespace).
    body = pae(PAYLOAD_TYPE, signed.envelope.payload)
    sig = subprocess.run(
        [signed.keygen.program, "-Y", "sign", "-q", "-f", str(signed.key.key), "-n", "git"],
        input=body,
        capture_output=True,
        check=True,
    ).stdout
    wrong_ns = Envelope(PAYLOAD_TYPE, signed.envelope.payload, (Signature("", sig),))
    with pytest.raises(ReleaseError, match="no signature"):
        verify_envelope(wrong_ns, signed.keygen, signed.key.allowed_signers)
    unsigned = Envelope(PAYLOAD_TYPE, signed.envelope.payload, ())
    with pytest.raises(ReleaseError, match="no signatures"):
        verify_envelope(unsigned, signed.keygen, signed.key.allowed_signers)


# R2: missing configuration says which key to set.
def test_signing_settings() -> None:
    conf = SigningSettings.from_section({})
    assert (conf.key, conf.allowed_signers, conf.ssh_keygen) == (None, None, "ssh-keygen")
    with pytest.raises(ConfigError, match=r"\[release.signing\]"):
        SigningSettings.from_section({"signing": {"key": 3}})
    with pytest.raises(ConfigError, match=r"\[release.signing\] key"):
        conf.require_key(None)
    assert conf.require_key(Path("/k")) == Path("/k")


# R3: only the envelope file, the allowed-signers file and the released blobs are needed.
def test_offline_verify(signed: Signed, tmp_path: Path) -> None:
    path = tmp_path / "release.intoto.json"
    path.write_text(signed.envelope.to_json())
    rel = signed.rel
    # No store, CAS or config is passed: nothing but these files is read.
    verified = verify_offline(
        path, signed.key.allowed_signers, [rel.tree, rel.report], keygen=signed.keygen
    )
    assert verified.principals == (signed.key.principal,)
    assert verified.subjects == {"lint.report": rel.report, "pack.srcs": rel.tree}

    with pytest.raises(ReleaseError, match=r"pack.srcs.*no blob"):
        verify_offline(path, signed.key.allowed_signers, [rel.report], keygen=signed.keygen)
    stray = tmp_path / "stray.txt"
    stray.write_text("not released")
    with pytest.raises(ReleaseError, match=r"stray.txt.*not a subject"):
        verify_offline(
            path, signed.key.allowed_signers, [rel.report, rel.tree, stray], keygen=signed.keygen
        )
    (rel.tree / "a.sv").write_text("module evil; endmodule\n")
    with pytest.raises(ReleaseError, match=r"pack.srcs"):
        verify_offline(
            path, signed.key.allowed_signers, [rel.report, rel.tree], keygen=signed.keygen
        )
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ReleaseError, match=r"bad.json"):
        verify_offline(bad, signed.key.allowed_signers, [], keygen=signed.keygen)


def test_check_subjects_without_blobs(signed: Signed) -> None:
    # No blobs: the signature alone is checked, every subject is reported unchecked.
    assert check_subjects(signed.statement, []) == {}


# --- R4: tampering with any field fails verification ----------------------------------------


def _paths(doc: Any, prefix: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    found = [prefix] if prefix else []
    if isinstance(doc, dict):
        for k, v in doc.items():
            found += _paths(v, (*prefix, k))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            found += _paths(v, (*prefix, i))
    return found


_VALUES = st.one_of(
    st.text(max_size=8),
    st.integers(-3, 3),
    st.booleans(),
    st.none(),
    st.just({"sha256": "0" * 64}),
    st.lists(st.integers(0, 2), max_size=2),
)


@st.composite
def _tampered(draw: st.DrawFn, statement: dict[str, Any]) -> dict[str, Any]:
    doc = copy.deepcopy(statement)
    path = draw(st.sampled_from(_paths(doc)))
    parent: Any = doc
    for part in path[:-1]:
        parent = parent[part]
    last = path[-1]
    op = draw(st.sampled_from(["set", "delete", "add"]))
    if op == "delete":
        del parent[last]
    elif op == "add" and isinstance(parent, dict):
        parent[draw(st.text(min_size=1, max_size=4)) + "_x"] = draw(_VALUES)
    else:
        old = parent[last]
        parent[last] = draw(_VALUES.filter(lambda v: v != old or type(v) is not type(old)))
    return doc


# R4: change, drop or add any field of the statement (subjects and predicate alike) while keeping
# the signature: verification fails. Each example runs ssh-keygen twice, hence `slow`.
@pytest.mark.slow
@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(data=st.data())
def test_tamper_detected(signed_once: Signed, data: st.DataObject) -> None:
    s = signed_once
    doc = data.draw(_tampered(s.statement))
    assert doc != s.statement or json.dumps(doc) != json.dumps(s.statement)
    encode = data.draw(st.sampled_from(["canonical", "pretty"]))
    payload = canonical_json(doc) if encode == "canonical" else json.dumps(doc, indent=1).encode()
    forged = Envelope(PAYLOAD_TYPE, payload, s.envelope.signatures)
    with pytest.raises(ReleaseError, match="no signature"):
        verify_envelope(Envelope.from_json(forged.to_json()), s.keygen, s.key.allowed_signers)


# R4: the subject digests in particular, and the payload type, are covered by the signature.
def test_tamper_subject_digest_and_type(signed: Signed) -> None:
    doc = copy.deepcopy(signed.statement)
    doc["subject"][0]["digest"]["sha256"] = "f" * 64
    forged = Envelope(PAYLOAD_TYPE, canonical_json(doc), signed.envelope.signatures)
    with pytest.raises(ReleaseError, match="no signature"):
        verify_envelope(forged, signed.keygen, signed.key.allowed_signers)
    retyped = Envelope("application/json", signed.envelope.payload, signed.envelope.signatures)
    with pytest.raises(ReleaseError, match="payloadType"):
        verify_envelope(retyped, signed.keygen, signed.key.allowed_signers)
    # The untouched envelope still verifies (the forgeries above failed for the right reason).
    assert verify_envelope(signed.envelope, signed.keygen, signed.key.allowed_signers).statement


# R2: what is signed is the DSSE PAE of the canonical payload, so any DSSE verifier (here plain
# ssh-keygen) accepts the signature.
def test_signature_covers_pae(signed: Signed, tmp_path: Path) -> None:
    stdins: list[tuple[list[str], bytes]] = []

    def recording(argv: Sequence[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
        stdins.append((list(argv), stdin))
        return run_ssh_keygen(argv, stdin)

    keygen = SshKeygen(signed.keygen.program, recording)
    envelope = sign_statement(signed.statement, SshSigner(keygen, signed.key.key))
    body = pae(PAYLOAD_TYPE, canonical_json(signed.statement))
    assert [s for a, s in stdins if "sign" in a] == [body]
    sig = tmp_path / "sig"
    sig.write_bytes(envelope.signatures[0].sig)
    plain = subprocess.run(
        [keygen.program, "-Y", "verify", "-f", str(signed.key.allowed_signers),
         "-I", signed.key.principal, "-n", "ebs-release", "-s", str(sig)],
        input=body, capture_output=True, check=False,
    )  # fmt: skip
    assert plain.returncode == 0, plain.stderr


# R2: with several signatures, only principals whose own signature verifies are reported, and
# a trusted signature after failing ones still verifies.
def test_multi_signature_principals(signed: Signed, tmp_path: Path) -> None:
    other = team_key(tmp_path / "b", principal="b-team@example.invalid")
    allowed = tmp_path / "allowed"
    allowed.write_text(signed.key.allowed_signers.read_text() + other.allowed_signers.read_text())
    payload = signed.envelope.payload
    good_b = SshSigner(signed.keygen, other.key).sign(pae(PAYLOAD_TYPE, payload))
    # B signed other bytes: its principal is found, but its signature does not verify.
    elsewhere = sign_statement({**signed.statement, "x": 1}, SshSigner(signed.keygen, other.key))
    mine = signed.envelope.signatures[0]
    env = Envelope(PAYLOAD_TYPE, payload, (elsewhere.signatures[0], mine))
    assert verify_envelope(env, signed.keygen, allowed).principals == (signed.key.principal,)
    both = Envelope(PAYLOAD_TYPE, payload, (good_b, mine))
    assert verify_envelope(both, signed.keygen, allowed).principals == (
        "b-team@example.invalid",
        signed.key.principal,
    )


# R2: an allowed-signers entry with several principals ("a,b") verifies.
def test_comma_separated_principals(signed: Signed, tmp_path: Path) -> None:
    line = signed.key.allowed_signers.read_text()
    allowed = tmp_path / "allowed"
    p = signed.key.principal
    allowed.write_text(line.replace(p, f"ops@example.invalid,{p}", 1))
    verified = verify_envelope(signed.envelope, signed.keygen, allowed)
    assert verified.principals == tuple(sorted(["ops@example.invalid", p]))


# R2: a trusted key's signature over anything but an in-toto v1 SLSA statement is refused.
def test_signed_non_slsa_payload_rejected(signed: Signed) -> None:
    signer = SshSigner(signed.keygen, signed.key.key)
    allowed = signed.key.allowed_signers
    changes = [
        {"predicateType": "https://example.invalid/other"},
        {"_type": "https://in-toto.io/Statement/v0.1"},
    ]
    for change in changes:
        env = sign_statement({**signed.statement, **change}, signer)
        with pytest.raises(ReleaseError, match="in-toto v1 statement"):
            verify_envelope(env, signed.keygen, allowed)
    array = b"[1, 2]"
    env = Envelope(PAYLOAD_TYPE, array, (signer.sign(pae(PAYLOAD_TYPE, array)),))
    with pytest.raises(ReleaseError, match="not an object"):
        verify_envelope(env, signed.keygen, allowed)


# R3: a tree manifest file matches its subject but is reported as manifest only.
def test_tree_manifest_is_manifest_only(signed: Signed, tmp_path: Path) -> None:
    rel = signed.rel
    root, manifests = build_tree(rel.tree, hash_file)
    manifest = tmp_path / "srcs.json"
    manifest.write_bytes(canonical_json(manifests[root].to_json()))
    path = tmp_path / "env.json"
    path.write_text(signed.envelope.to_json())
    allowed = signed.key.allowed_signers
    v = verify_offline(path, allowed, [rel.report, manifest], keygen=signed.keygen)
    assert v.subjects["pack.srcs"] == manifest
    assert v.manifest_only == ("pack.srcs",)
    full = verify_offline(path, allowed, [rel.report, rel.tree], keygen=signed.keygen)
    assert full.manifest_only == ()


# R3: a subject with several digests matches only when every supported one matches.
def test_subject_multi_algo(signed: Signed) -> None:
    pytest.importorskip("blake3", reason="missing dependency: blake3 (uv sync --all-extras)")
    report = signed.rel.report
    sha = hash_file(report)[0].hex
    b3 = hash_file(report, "blake3")[0].hex

    def stmt(digest: dict[str, str]) -> dict[str, Any]:
        return {"subject": [{"name": "r", "digest": digest}]}

    assert check_subjects(stmt({"sha256": sha, "blake3": b3}), [report]) == {"r": report}
    assert check_subjects(stmt({"blake3": b3}), [report]) == {"r": report}
    with pytest.raises(ReleaseError, match="subject r"):
        check_subjects(stmt({"sha256": sha, "blake3": "0" * 64}), [report])


# R2, R3: errors say what is wrong.
def test_error_paths(signed: Signed, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        verify_envelope(signed.envelope, signed.keygen, tmp_path / "missing")
    with pytest.raises(ReleaseError, match="signing with"):
        sign_statement(signed.statement, SshSigner(signed.keygen, tmp_path / "no-key"))
    with pytest.raises(ReleaseError, match="invalid base64"):
        Envelope.from_json('{"payloadType": "x", "payload": "!!!", "signatures": []}')
    principal = signed.key.principal.encode()

    def lying(argv: Sequence[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
        if "find-principals" in argv:  # fails, yet prints a principal
            return subprocess.CompletedProcess(list(argv), 255, principal, b"")
        return subprocess.CompletedProcess(list(argv), 0, b"", b"")

    with pytest.raises(ReleaseError, match="no signature"):
        verify_envelope(signed.envelope, SshKeygen("ssh-keygen", lying), signed.key.allowed_signers)
    missing = SshSigner(SshKeygen(str(tmp_path / "nope")), signed.key.key)
    with pytest.raises(ConfigError, match="OpenSSH"):
        sign_statement(signed.statement, missing)
