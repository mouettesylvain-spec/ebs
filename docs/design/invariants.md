# Invariants

A wrong cache hit is the worst possible bug in this system: it silently returns stale results and
destroys user trust. Every invariant below has a named test. A task that touches the area must keep
that test green, and a PR that weakens one needs explicit human approval.

| ID | Invariant | Enforcing test(s) | Introduced by |
| --- | --- | --- | --- |
| I1 | Canonical JSON is deterministic: equal values ⇒ equal bytes, regardless of dict insertion order | `tests/unit/core/test_canon.py::test_order_independence` (Hypothesis) | P0-02 |
| I2 | Canonical JSON is injective on its domain: different values ⇒ different bytes | `test_canon.py::test_injective` (Hypothesis) | P0-02 |
| I3 | Every field in the key document changes the key; no excluded field (resources, licenses, debug, user, time, absolute paths, domain) does | `tests/unit/plan/test_key_sensitivity.py` (Hypothesis, field-by-field mutation) | P0-08 |
| I4 | Golden keys are stable across releases unless `KEY_SCHEMA_VERSION` is bumped | `tests/unit/plan/test_golden_keys.py` (fixtures in `tests/fixtures/golden_keys/`) | P0-08 |
| I5 | Tree digest depends on names, content, executable bit and symlink targets only; not on mtime, owner, directory enumeration order | `tests/unit/core/test_tree.py::test_metadata_independence` | P0-03 |
| I6 | Rerun decisions never use timestamps; the stat cache only skips rehashing and is guarded by the racy-clean rule | `tests/unit/sources/test_statcache.py::test_racy_clean`, `::test_same_mtime_different_content` | P0-06 |
| I7 | Actions execute on the plan-time snapshot bytes, never on live source paths | `tests/integration/test_snapshot_isolation.py` (edit file during build) | P0-06 / P0-13 |
| I8 | A CAS object is visible only when complete; concurrent writers of the same digest both succeed and leave one valid object | `tests/unit/cas/test_fs_atomic.py`, `tests/integration/test_cas_concurrency.py` (multiprocess) | P0-09 |
| I9 | A tree manifest is written only after all its blobs and child manifests | `tests/unit/cas/test_fs_tree_order.py` (fault injection) | P0-09 |
| I10 | The runner verifies input digests before executing; mismatch ⇒ exit 76, no result posted | `tests/unit/runner/test_verify_inputs.py` | P0-13 |
| I11 | Only declared env vars + toolchain env reach the tool; HOME is an empty scratch dir | `tests/unit/runner/test_env_scrub.py` | P0-13 |
| I12 | Infrastructure failures are never cached; test failures are | `tests/unit/driver/test_failure_classes.py` | P0-16 |
| I13 | Cache lookups are scoped by domain; a key present in domain A is a miss in domain B | `tests/contract/test_metadata_store.py::test_domain_scoping` | P0-10 |
| I14 | Nondeterministic outputs pass `nondeterministic_output_id(producer_key, name)` downstream; re-running the producer does not change downstream keys | `tests/unit/plan/test_nondeterministic.py` | P0-08 |
| I15 | GC never deletes an object reachable from a release, a pin or a live lease; action-cache rows are removed before their blobs, after a grace period | `tests/unit/gc/test_mark.py`, `tests/integration/test_gc_concurrent_build.py` | P1-09 |
| I16 | Node scratch is removed on success, failure, timeout and SIGTERM | `tests/integration/test_runner_cleanup.py` (fake SLURM kill) | P1-04 |
| I17 | No subprocess is started with `shell=True` using interpolated user data | `tests/unit/test_no_shell_true.py` (AST scan of src/) | P0-01 |
| I18 | Package layering (overview.md) holds | `tests/unit/test_layering.py` (import-linter contracts) | P0-01 |
| I19 | Released outputs are GC roots and releases are never deleted (only yanked) | `tests/unit/release/test_release_immutability.py` | P2-02 |
| I20 | A user can never read cache entries, logs or provenance of a domain they are not a member of, through any API | `tests/integration/test_authz_matrix.py` | P2-01 |

## Mutation check

The nightly pipeline runs `mutmut` on `ebs/core/canon.py`, `ebs/core/tree.py`, `ebs/plan/keys.py`
and `ebs/sources/statcache.py`. Surviving mutants in these files are treated as bugs in the tests.
