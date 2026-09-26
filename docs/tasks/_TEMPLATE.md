# PX-NN — Title

Status: todo · Phase: X · Depends on: … · Parallel-safe with: … · Size: S | M | L

## Goal
One or two sentences: what exists after this task that didn't before, and why it matters.

## Read first
- docs/architecture.md § "…" (only the named section)
- docs/design/interfaces.md § N
- Existing code to follow: `src/ebs/…`

## Scope (files)
- create `src/ebs/…`
- create `tests/unit/…`
- modify `…` (only these lines/functions)

## Out of scope
Things a reasonable engineer might add but must not, with the task that owns them.

## Contract
Signatures this task must provide exactly (copy from interfaces.md or define here and add there).

## Requirements
- R1 …
- R2 …

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `tests/unit/…::test_…` | R1 | unit |

## Done when
- [ ] `make check` passes (paste the summary line)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] interfaces.md / invariants.md updated if a contract or invariant was added
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
Pitfalls, decisions taken under an open decision (Dx), follow-ups discovered.
