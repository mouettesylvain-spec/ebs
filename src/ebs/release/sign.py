"""Signed release attestations: DSSE envelopes signed with `ssh-keygen -Y` (P3-03 R2-R4).

`sign_statement` serializes a statement (`ebs.release.attest`) as canonical JSON, the DSSE
payload, and signs its pre-authentication encoding (`pae`, DSSE v1) with the team's SSH key:
`ssh-keygen -Y sign -f <key> -n ebs-release`. The envelope is JSON:

    {"payloadType": "application/vnd.in-toto+json", "payload": "<base64 statement>",
     "signatures": [{"keyid": "SHA256:<key fingerprint>", "sig": "<base64 SSH signature>"}]}

`verify_envelope` needs only the envelope and an allowed-signers file (`ssh-keygen(1)` ALLOWED
SIGNERS): each signature's principals come from `ssh-keygen -Y find-principals`, then
`ssh-keygen -Y verify -n ebs-release` checks it; at least one must verify. The statement is read
from the verified payload bytes only, so changing any byte of it (a subject digest, a predicate
field) fails. `verify_offline` also checks released blobs (files, tree directories or tree
manifests) against the subject digests. No store, network or config is involved.

Configuration (`[release.signing]` in ebs.toml): `key` (the team's private key, or its `.pub`
when the key lives in ssh-agent), `allowed_signers`, `ssh_keygen` (default `ssh-keygen` on PATH).
GPG signing is not implemented (follow-up).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TypeAlias

from ebs.core.canon import canonical_json
from ebs.core.digest import ALGOS, Algo, Digest, hash_file
from ebs.core.errors import ConfigError, DigestError, EbsError, ReleaseError
from ebs.core.tree import build_tree
from ebs.core.types import JsonValue
from ebs.release.attest import PREDICATE_TYPE, STATEMENT_TYPE

__all__ = [
    "NAMESPACE",
    "PAYLOAD_TYPE",
    "Envelope",
    "Signature",
    "SigningSettings",
    "SshKeygen",
    "SshRunner",
    "SshSigner",
    "Verified",
    "check_subjects",
    "manifest_only",
    "pae",
    "run_ssh_keygen",
    "sign_statement",
    "verify_envelope",
    "verify_offline",
]

PAYLOAD_TYPE: Final = "application/vnd.in-toto+json"
NAMESPACE: Final = "ebs-release"

SshRunner: TypeAlias = Callable[[Sequence[str], bytes], "subprocess.CompletedProcess[bytes]"]
"""Runs ssh-keygen: argv list and stdin bytes -> the completed process (never raises on exit)."""


def run_ssh_keygen(argv: Sequence[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(argv), input=stdin, capture_output=True, check=False)


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE v1 pre-authentication encoding: what is actually signed."""
    kind = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(kind), kind, len(payload), payload)


# --- configuration ---------------------------------------------------------------------------


def _path(section: Mapping[str, object], key: str) -> Path | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"[release.signing] {key} must be a file path, got {value!r}")
    return Path(value).expanduser()


@dataclass(frozen=True, slots=True)
class SigningSettings:
    """`[release.signing]`: the team key, the allowed-signers file and the ssh-keygen program."""

    key: Path | None = None
    allowed_signers: Path | None = None
    ssh_keygen: str = "ssh-keygen"

    @classmethod
    def from_section(cls, section: Mapping[str, object]) -> SigningSettings:
        """From the `[release]` config section."""
        raw = section.get("signing", {})
        if not isinstance(raw, Mapping):
            raise ConfigError("[release.signing] must be a table (key, allowed_signers, ...)")
        program = raw.get("ssh_keygen", "ssh-keygen")
        if not isinstance(program, str) or not program:
            raise ConfigError(f"[release.signing] ssh_keygen must be a program, got {program!r}")
        return cls(_path(raw, "key"), _path(raw, "allowed_signers"), program)

    def require_key(self, override: Path | None) -> Path:
        key = override or self.key
        if key is None:
            raise ConfigError(
                'no signing key: set [release.signing] key = "<path to the team\'s SSH key>" '
                "in ebs.toml, or pass --key"
            )
        return key

    def require_allowed_signers(self, override: Path | None) -> Path:
        found = override or self.allowed_signers
        if found is None:
            raise ConfigError(
                "no allowed-signers file: pass --allowed-signers FILE, or set "
                '[release.signing] allowed_signers = "<path>" in ebs.toml'
            )
        return found


# --- ssh-keygen ------------------------------------------------------------------------------


def _stderr(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stderr.decode(errors="replace").strip() or f"exit status {proc.returncode}"


@dataclass(frozen=True)
class SshKeygen:
    """`ssh-keygen -Y` and `-l`, through an injected runner."""

    program: str = "ssh-keygen"
    run: SshRunner = field(default=run_ssh_keygen)

    def _call(self, args: Sequence[str], stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        try:
            return self.run([self.program, *args], stdin)
        except FileNotFoundError:
            raise ConfigError(
                f"cannot run {self.program!r}: install OpenSSH >= 8.2 or set "
                "[release.signing] ssh_keygen to its path"
            ) from None

    def sign(self, key: Path, data: bytes) -> bytes:
        proc = self._call(["-Y", "sign", "-q", "-f", str(key), "-n", NAMESPACE], data)
        if proc.returncode != 0 or not proc.stdout:
            raise ReleaseError(
                f"signing with {key} failed: {_stderr(proc)}; check [release.signing] key (a "
                "private key without passphrase prompt, or a .pub whose key is in ssh-agent)"
            )
        return proc.stdout

    def fingerprint(self, key: Path) -> str:
        proc = self._call(["-l", "-f", str(key)])
        fields = proc.stdout.decode(errors="replace").split()
        return fields[1] if proc.returncode == 0 and len(fields) > 1 else ""

    def find_principals(self, allowed_signers: Path, sig_file: Path) -> list[str]:
        proc = self._call(
            ["-Y", "find-principals", "-s", str(sig_file), "-f", str(allowed_signers)]
        )
        if proc.returncode != 0:
            return []
        # One line per matching allowed-signers entry; an entry may list principals "a,b".
        lines = proc.stdout.decode(errors="replace").splitlines()
        return [p.strip() for line in lines for p in line.split(",") if p.strip()]

    def verify(self, allowed_signers: Path, principal: str, sig_file: Path, data: bytes) -> bool:
        args = ["-Y", "verify", "-f", str(allowed_signers), "-I", principal, "-n", NAMESPACE]
        return self._call([*args, "-s", str(sig_file)], data).returncode == 0


@dataclass(frozen=True)
class SshSigner:
    """Signs with one key file through `keygen`."""

    keygen: SshKeygen
    key: Path

    def sign(self, data: bytes) -> Signature:
        return Signature(self.keygen.fingerprint(self.key), self.keygen.sign(self.key, data))


# --- envelopes -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signature:
    keyid: str  # advisory: the signing key's fingerprint; verification never trusts it
    sig: bytes  # the armored SSH signature


@dataclass(frozen=True, slots=True)
class Envelope:
    """A DSSE envelope: payload type, payload bytes and signatures over `pae(type, payload)`."""

    payload_type: str
    payload: bytes
    signatures: tuple[Signature, ...]

    def to_json(self) -> str:
        doc = {
            "payloadType": self.payload_type,
            "payload": base64.b64encode(self.payload).decode(),
            "signatures": [
                {"keyid": s.keyid, "sig": base64.b64encode(s.sig).decode()} for s in self.signatures
            ],
        }
        return json.dumps(doc, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str, *, source: str = "envelope") -> Envelope:
        def bad(why: str) -> ReleaseError:
            return ReleaseError(f"{source} is not a DSSE envelope: {why}")

        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise bad(f"invalid JSON ({exc})") from None
        if not isinstance(doc, dict):
            raise bad("expected a JSON object")
        kind, payload, sigs = doc.get("payloadType"), doc.get("payload"), doc.get("signatures")
        if not isinstance(kind, str) or not isinstance(payload, str):
            raise bad("payloadType and payload must be strings")
        if not isinstance(sigs, list):
            raise bad("signatures must be a list")
        try:
            body = base64.b64decode(payload, validate=True)
            parsed: list[Signature] = []
            for s in sigs:
                if not isinstance(s, dict) or not isinstance(s.get("sig"), str):
                    raise bad("each signature needs a base64 'sig'")
                keyid = s.get("keyid", "")
                parsed.append(
                    Signature(
                        keyid if isinstance(keyid, str) else "",
                        base64.b64decode(s["sig"], validate=True),
                    )
                )
        except binascii.Error as exc:
            raise bad(f"invalid base64 ({exc})") from None
        return cls(kind, body, tuple(parsed))


def sign_statement(statement: Mapping[str, JsonValue], signer: SshSigner) -> Envelope:
    """`statement` as a DSSE envelope signed by `signer` (namespace `ebs-release`)."""
    payload = canonical_json(dict(statement))
    return Envelope(PAYLOAD_TYPE, payload, (signer.sign(pae(PAYLOAD_TYPE, payload)),))


@dataclass(frozen=True)
class Verified:
    """A verified attestation: its statement, the principals whose signatures verified and,
    with `verify_offline`, the subject name -> blob that matched its digest."""

    statement: dict[str, JsonValue]
    principals: tuple[str, ...]
    subjects: dict[str, Path] = field(default_factory=dict)
    manifest_only: tuple[str, ...] = ()  # tree subjects matched by their manifest file only


def verify_envelope(envelope: Envelope, keygen: SshKeygen, allowed_signers: Path) -> Verified:
    """Check the envelope's signatures against `allowed_signers`; the statement it signs."""
    if envelope.payload_type != PAYLOAD_TYPE:
        raise ReleaseError(
            f"envelope payloadType is {envelope.payload_type!r}, expected {PAYLOAD_TYPE!r}"
        )
    if not envelope.signatures:
        raise ReleaseError("the envelope has no signatures; sign it with `ebs release attest`")
    if not allowed_signers.is_file():
        raise ConfigError(f"allowed-signers file {allowed_signers} does not exist")
    body = pae(envelope.payload_type, envelope.payload)
    principals: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="ebs-verify-") as tmp:
        for i, sig in enumerate(envelope.signatures):
            sig_file = Path(tmp, f"{i}.sig")
            sig_file.write_bytes(sig.sig)
            for principal in keygen.find_principals(allowed_signers, sig_file):
                if keygen.verify(allowed_signers, principal, sig_file, body):
                    principals.add(principal)
    if not principals:
        raise ReleaseError(
            f"no signature of the envelope verifies against an allowed signer of "
            f"{allowed_signers} (namespace {NAMESPACE}): the attestation was changed after "
            "signing, or its key is not trusted there"
        )
    try:
        statement = json.loads(envelope.payload)
    except ValueError as exc:
        raise ReleaseError(f"the signed payload is not JSON: {exc}") from None
    if not isinstance(statement, dict):
        raise ReleaseError("the signed payload is not an in-toto statement (not an object)")
    if statement.get("_type") != STATEMENT_TYPE or statement.get("predicateType") != (
        PREDICATE_TYPE
    ):
        raise ReleaseError(
            f"the signed payload is not an in-toto v1 statement with SLSA provenance v1 "
            f"(_type {statement.get('_type')!r}, predicateType {statement.get('predicateType')!r})"
        )
    return Verified(statement, tuple(sorted(principals)))


# --- subjects --------------------------------------------------------------------------------


def _subjects(statement: Mapping[str, JsonValue]) -> dict[str, list[Digest]]:
    raw = statement.get("subject")
    if not isinstance(raw, list):
        raise ReleaseError("the statement has no subject list")
    found: dict[str, list[Digest]] = {}
    for s in raw:
        digests = s.get("digest") if isinstance(s, dict) else None
        name = s.get("name") if isinstance(s, dict) else None
        if not isinstance(name, str) or not isinstance(digests, dict):
            raise ReleaseError(f"statement subject {s!r} needs a name and a digest")
        parsed: list[Digest] = []
        for algo, hex_ in digests.items():
            if algo in ALGOS and isinstance(hex_, str):
                with contextlib.suppress(DigestError):  # a bad value: as if absent
                    parsed.append(Digest(algo, hex_))  # type: ignore[arg-type]  # in ALGOS
        if not parsed:
            raise ReleaseError(
                f"subject {name}: no digest with a supported algorithm ({sorted(ALGOS)})"
            )
        found[name] = parsed
    return found


def _blob_digest(path: Path, algo: Algo) -> Digest:
    try:
        if path.is_dir():
            return build_tree(path, lambda p: hash_file(p, algo))[0]
        return hash_file(path, algo)[0]
    except OSError as exc:
        raise ReleaseError(f"cannot read released blob {path}: {exc.strerror}") from None


def check_subjects(statement: Mapping[str, JsonValue], blobs: Sequence[Path]) -> dict[str, Path]:
    """Match `blobs` (files, tree directories or tree manifests) to the statement's subjects.

    Every subject needs a blob with all its (supported) digests and every blob must be a
    subject; no blobs at all checks nothing (returns {}). A tree subject matched by a file is
    its manifest only: the tree's file contents were not checked (`manifest_only`).
    """
    if not blobs:
        return {}
    subjects = _subjects(statement)
    algos = sorted({d.algo for ds in subjects.values() for d in ds})
    matched: dict[str, Path] = {}
    strays: list[str] = []
    for blob in blobs:
        try:
            have = {_blob_digest(blob, a) for a in algos}
        except EbsError as exc:
            raise ReleaseError(f"cannot hash released blob {blob}: {exc}") from None
        # Every supported digest of a subject must match: one wrong algorithm is a mismatch.
        names = [n for n, ds in subjects.items() if all(d in have for d in ds)]
        if not names:
            strays.append(f"{blob}: not a subject of this attestation (its digest matches none)")
        for name in names:
            matched.setdefault(name, blob)
    problems = [
        f"subject {n} ({', '.join(map(str, subjects[n]))}): no blob given has this digest"
        for n in subjects
        if n not in matched
    ] + strays
    if problems:
        raise ReleaseError("released blobs do not match the attestation: " + "; ".join(problems))
    return matched


def verify_offline(
    envelope_file: Path, allowed_signers: Path, blobs: Sequence[Path], *, keygen: SshKeygen
) -> Verified:
    """Verify an envelope file, then the blobs against its subjects (R3)."""
    try:
        text = envelope_file.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"cannot read envelope {envelope_file}: {exc}") from None
    envelope = Envelope.from_json(text, source=os.fspath(envelope_file))
    verified = verify_envelope(envelope, keygen, allowed_signers)
    subjects = check_subjects(verified.statement, blobs)
    return Verified(
        verified.statement,
        verified.principals,
        subjects,
        manifest_only(verified.statement, subjects),
    )


def manifest_only(
    statement: Mapping[str, JsonValue], subjects: Mapping[str, Path]
) -> tuple[str, ...]:
    """The tree subjects (annotation `type: tree`) that a file, their manifest, matched."""
    raw = statement.get("subject")
    trees = {
        s.get("name")
        for s in (raw if isinstance(raw, list) else [])
        if isinstance(s, dict)
        and isinstance(s.get("annotations"), dict)
        and s["annotations"].get("type") == "tree"  # type: ignore[union-attr]
    }
    return tuple(sorted(n for n, p in subjects.items() if n in trees and not p.is_dir()))
