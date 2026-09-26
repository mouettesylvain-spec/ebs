# P3-03 — SLSA attestations and signing

Status: todo · Phase: 3 · Depends on: P2-02 · Size: M

## Goal
Each release carries a signed in-toto statement with the SLSA v1 provenance predicate, verifiable offline
years later.

## Read first
- docs/architecture.md § "Versioning…" → Attestations; https://slsa.dev/spec/v1.0/provenance

## Scope (files)
- create `src/ebs/release/attest.py`, `sign.py` (ssh-keygen -Y sign/verify; GPG optional), CLI `ebs release attest`,
  `ebs release verify-signature`; tests `tests/unit/release/test_attest.py`, `test_sign.py`

## Requirements
- R1 Statement: subjects = released outputs (name + sha256); predicateType SLSA provenance v1;
  `buildDefinition.externalParameters` = flow path/commit, params digest, lock digest; `resolvedDependencies` =
  imports (release digests) and toolchains (ids); `runDetails` = builder id (`ebs@<version>`), build UUID, timestamps.
- R2 Serialized as a DSSE envelope; signature with `ssh-keygen -Y sign -n ebs-release` using a team key path from
  config; verification with an allowed-signers file.
- R3 `verify-signature` works offline with only the envelope, allowed-signers file and the released blobs.
- R4 Tampering with any subject digest or predicate field fails verification (property test over fields).

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_attest.py::test_statement_schema` (validate against vendored SLSA/in-toto JSON schemas) | R1 | unit |
| `test_sign.py::test_sign_verify_roundtrip` (temp ssh key) | R2 | unit |
| `test_sign.py::test_offline_verify` | R3 | unit |
| `test_sign.py::test_tamper_detected` | R4 | property |
