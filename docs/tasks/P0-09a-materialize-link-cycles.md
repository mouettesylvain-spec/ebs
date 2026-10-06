# P0-09a — Materialize: symlink cycles through missing directories

Status: todo · Phase: 0 · Depends on: P0-09 · Parallel-safe with: P0-16, P1-01, P1-08 · Size: S

## Goal
`tests/unit/cas/test_materialize.py::test_resolve_matches_kernel` (P0-09 R7) has a Hypothesis
counterexample, so `make check` fails on every run (Hypothesis replays the saved example). Decide
who is wrong, the escape check `ebs.cas.materialize._resolve` or the test's oracle, fix it, and
make the property state precisely what it guarantees. This guards against symlink escapes when
trees are materialized on nodes, so the answer must be proven, not assumed.

## Read first
- docs/tasks/P0-09-cas-fs.md: R7 and the Notes on link budgets (ELOOP, userspace resolvers)
- docs/tasks/P0-03-tree-manifests.md: Notes on symlink escapes (lexical + realpath checks)
- Existing code: `src/ebs/cas/materialize.py` (`_resolve`, `_walk`, `_MAX_HOPS`),
  `tests/unit/cas/test_materialize.py` (`_link_farms`, `test_resolve_matches_kernel`)

## The counterexample
```
dirs  = []                                   # no directory `d` exists
links = {"l0": "l1/..", "l1": "d/../l0/.."}  # both at the tree root
```
- `_resolve((), "l1/..", table)` follows l1 → `d/../l0/..` → l0 → `l1/..` → … until the
  40-hop budget runs out, and then returns the base `()`: **inside**.
- The oracle `os.path.realpath(root / "l0")` (non-strict) resolves the missing `d` lexically and
  stops at the loop in its own way, landing **outside** the root.
- The kernel: `os.stat` of `l0` and of `l1` both fail with `ENOENT` (checked on 2026-10-06:
  the lookup of the missing `d` fails), while `realpath` returns the root's parent for both.
  The oracle only excludes `ELOOP` (`assume`), so the example is kept.

Working hypothesis (to verify, not to assume): on disk this link cannot be followed at all
(ENOENT or ELOOP), so it cannot escape, and the oracle, not `_resolve`, is wrong: Python's
non-strict `realpath` is not a model of the kernel for dangling or looping links.

## Scope (files)
- modify `tests/unit/cas/test_materialize.py` (oracle and/or new tests)
- modify `src/ebs/cas/materialize.py` only if R1 shows `_resolve` is unsafe
- update `docs/tasks/P0-09-cas-fs.md` Notes with the finding

## Out of scope
Sandboxing (P3-01), `O_NOFOLLOW` writes during materialization (already required by P0-09's notes),
changing `_MAX_HOPS`.

## Contract
No public API change. `_resolve(base, target, links) -> tuple[str, ...] | None` keeps its meaning:
None means "following this link can land outside the tree".

## Requirements
- R1 Establish the kernel's real behaviour for the counterexample on disk (errno of `os.stat`,
  `os.open` with and without `O_NOFOLLOW`, and whether *any* path through the root that uses
  `l0`/`l1` reaches outside), and record it in the task Notes with the commands used.
- R2 If the kernel can never reach outside through such links (expected), the oracle must model
  the kernel: an `os.stat`/`os.open` failure (ENOENT, ELOOP, ENOTDIR) means "cannot escape on
  disk", and the property asserts that `_resolve` never says "inside" for a link that does
  escape on disk (the safety direction), plus agreement whenever the link resolves on disk.
  `os.path.realpath(strict=False)` must not be the oracle for links that do not resolve.
- R3 If the kernel *can* escape (unexpected), `_resolve` returns None for that case, and every
  existing materialize test still passes.
- R4 The counterexample becomes an explicit `@example` (or a named unit test) so it is checked on
  every run, not only when Hypothesis happens to find it.
- R5 No weakening: the property keeps `max_examples=150`, and the only examples it discards are
  ones where the disk result itself is undefined for the oracle, with the reason in a comment.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_materialize.py::test_link_cycle_through_missing_dir_on_disk` (asserts the errno and that nothing outside is reachable) | R1 | unit |
| `test_materialize.py::test_resolve_matches_kernel` with `@example` of the counterexample | R2, R4, R5 | property |
| `test_materialize.py::test_resolve_never_says_inside_when_disk_escapes` (safety direction) | R2, R3 | property |

## Done when
- [ ] `make check` passes (paste the summary line); `rm -rf .hypothesis` then `make check` passes too
- [ ] every R has a test; task-reviewer and test-critic report no correctness gaps
- [ ] P0-09 Notes updated with the finding
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
Found while running `make check` for P0-15 (2026-10-06): reproduced on a clean `main` checkout,
so it predates P0-15. Until this task lands, `make check` fails on that one test on any machine
whose `.hypothesis/` database holds the example (CI starts clean and may pass by chance).
