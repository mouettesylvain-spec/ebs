# EBS agent kit

Drop the contents of this folder at the root of the new `ebs` repository (empty repo is fine) and commit
it before the first task. It turns the architecture plan into work Claude Code agents can execute one
task at a time, test-first.

```
CLAUDE.md                     always-loaded project memory (commands, workflow, conventions, invariants) — 68 lines
.claude/settings.json         permission allowlist/denylist + hooks (auto-format on edit, tests must pass on stop)
.claude/rules/*.md            path-scoped rules, loaded only when an agent touches matching files
.claude/agents/               task-reviewer (diff vs task spec), test-critic (mutation-style test gaps)
.claude/skills/               /implement-task <ID>, /review-task <ID>
scripts/claude-*.sh           the hook scripts
docs/architecture.md          the full architecture plan (exported from the Claude Doc)
docs/design/                  contracts agents build against: overview, interfaces, invariants, data model, testing
docs/tasks/                   47 task files (phases 0–3) + phase 4 backlog, index with dependency waves
```

## How to run it

1. **Bootstrap.** In the repo: `claude`, then `/implement-task P0-01`. Review and merge. From here on
   `make check` exists and the Stop hook enforces it.
2. **Parallel waves.** docs/tasks/README.md groups tasks into waves whose members touch disjoint packages.
   Run one session per task, each in its own worktree: `claude --worktree task/P0-02`, `/implement-task P0-02`.
   Start a task only when everything in its "Depends on" line is merged.
3. **Review.** When a task is in `review`, start a fresh session and run `/review-task <ID>`, then do your own
   human review of the MR. You set `done` after merging.
4. **Between tasks** use `/clear` (or a new session). One task per context keeps agents accurate.
5. **Open decisions D1–D7** (docs/tasks/README.md) have defaults so work isn't blocked; confirm each before the
   task listed next to it.

## Why it is shaped this way

- CLAUDE.md stays short and generic; detail lives in files loaded on demand (design docs, path-scoped rules,
  skills), following Claude Code's guidance that long always-on instructions get ignored.
- Every task has numbered requirements, and every requirement maps to a named test. The reviewer subagent checks
  that mapping in a fresh context, and the test-critic hunts for tests that would miss a bug in the parts where
  a bug means a wrong cache hit.
- Deterministic guardrails (hooks, permission deny rules, import-linter layering, the no-`shell=True` scan,
  golden action keys) cover what must never be left to judgment.
- Interfaces are fixed up front in docs/design/interfaces.md so parallel agents can build against each other's
  contracts using the shared fakes (in-memory metadata store, FakeSlurm, fake Questa tools, tmp CAS).
