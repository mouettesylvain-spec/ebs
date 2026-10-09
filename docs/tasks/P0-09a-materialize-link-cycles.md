# P0-09a — Materialize: symlink cycles through missing directories

Status: done · Phase: 0 · Depends on: P0-09 · Parallel-safe with: P0-16, P1-01, P1-08 · Size: S

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
- [x] `make check` passes (paste the summary line); `rm -rf .hypothesis` then `make check` passes too
- [x] every R has a test; task-reviewer and test-critic report no correctness gaps
- [x] P0-09 Notes updated with the finding
- [x] Status set to `review` here and in docs/tasks/README.md

## Notes
Found while running `make check` for P0-15 (2026-10-06): reproduced on a clean `main` checkout,
so it predates P0-15. Until this task lands, `make check` fails on that one test on any machine
whose `.hypothesis/` database holds the example (CI starts clean and may pass by chance).

### Finding (R1, 2026-10-06, Linux 5.15 / WSL2, Python 3.11)
Farm at `<tmp>/outer/root` with `l0 -> l1/..`, `l1 -> d/../l0/..`, no `d`. Probed with `os.stat`,
`os.open(p, O_RDONLY)`, `os.open(p, O_RDONLY|O_NOFOLLOW)` and `os.open(p, O_PATH|O_NOFOLLOW)` +
`readlink /proc/self/fd/N`, for `l0`, `l1`, `l0/..`, `l1/..`, `l0/x`, `l1/../..`, `l0/../l1`:

| | `stat` / `open` | `open(O_NOFOLLOW)` on `l0`/`l1` | `realpath(strict=False)` |
| --- | --- | --- | --- |
| no `d` | ENOENT for every path (the lookup of `root/d` fails) | ELOOP | `<tmp>/outer` (outside) |
| `d` created as a dir | ELOOP for every path | ELOOP | `<tmp>/outer` (outside) |

The kernel never follows either link, in either state, so nothing outside is reachable through
them. `_resolve` (inside) is right; the oracle was wrong: non-strict `realpath` resolves a missing
`d` lexically and stops at the loop on its own terms. R3 does not apply, `materialize.py` is
unchanged. `test_link_cycle_through_missing_dir_on_disk` pins the errnos and checks every path of
up to 3 components through `l0`/`l1`.

New oracle: the kernel itself (`os.open(O_DIRECTORY)` + `/proc/self/fd`, so the tests are
Linux-only like the existing hop-budget test). `test_resolve_matches_kernel` compares every link
that resolves on disk and drops the `assume` (no example is discarded).
`test_resolve_never_says_inside_when_disk_escapes` checks the worst case at runtime: every directory
a lookup is missing is created (named by `realpath(strict=True)`, which walks in the kernel's
order), and a lookup that needs a directory outside the work dir counts as an escape. On that disk
`_resolve`'s model (missing names are plain dirs) is exact, so it also asserts agreement; only
ELOOP leaves no answer. Farm dirs are mirrored in `outer/`, so escaping links mostly resolve
instead of dangling. Mutants checked: ignoring `..` past the root and never following links fail
both properties; reporting ELOOP as an escape fails the R1 test.

### Review (task-reviewer, test-critic)
task-reviewer: no blockers. test-critic found five mutants that make `materialize` accept an
escaping link while every test passed; each now has a test that fails:
- budget charged per name instead of per link follow: `test_resolve_budget_counts_links_not_components`
- a link seen twice in one lookup treated as a loop: `@example` `l0 -> .`, `x -> l0/l0/..`
- an empty component (`d//..`) treated as a name: `@example` `d//../..`, plus a trailing-slash example
- only the last link checked, or each link checked against an incomplete table while the tree is
  filled: `test_escape_through_later_link_rejected`

It also asked for an `@example` that climbs above the work dir, so that `_complete`'s "outside" branch
runs. Not addressed: the kernel tests need `/proc/self/fd` (Linux only, like the 40-hop test). The
known limit stays: a tool that resolves a path piecewise gets a fresh 40-hop budget at each step,
so an ELOOP chain could be followed out that way. This is the existing P0-09 follow-up about acyclic
over-budget chains.
