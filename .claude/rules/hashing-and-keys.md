---
paths:
  - "src/ebs/core/**"
  - "src/ebs/plan/**"
  - "src/ebs/sources/**"
  - "src/ebs/toolchain/**"
---
# Rules for hashing, keys and sources

You are editing code where a bug silently returns wrong cache results. Before changing anything:

- Read docs/design/invariants.md and identify which invariants your change touches (I1–I7, I14).
- Any change to what goes into `action_key_document`, to `canonical_json`, to tree manifests or to
  input-id derivation changes keys: bump `KEY_SCHEMA_VERSION`, regenerate
  `tests/fixtures/golden_keys/` with `uv run python scripts/regen_golden_keys.py`, and explain why in the
  commit body. Never regenerate golden keys to "fix" a failing test without that bump.
- Never use mtime/ctime to decide whether something changed; only to skip rehashing under the stat-cache
  guards.
- Add a Hypothesis property test for every new canonicalization, ordering or parsing function.
- Keep these modules pure: no network, no subprocess (except `sources/gitids.py` and `toolchain/env.py`,
  which use the injected runner), no global state.
