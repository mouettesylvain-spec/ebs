---
name: implement-task
description: Implement one EBS task file from docs/tasks end to end with tests first. Use when the user runs /implement-task <ID>.
disable-model-invocation: true
---
Implement task $ARGUMENTS.

1. Read `docs/tasks/$ARGUMENTS*.md` and confirm every task in its "Depends on" line is `done` in
   docs/tasks/README.md. If not, stop and say which ones are missing.
2. Read only the "Read first" sections and the existing code the task names. Use a subagent if you
   need to explore more widely.
3. Create branch `task/$ARGUMENTS-<slug>` if you are on main. Set Status to `in-progress` in the task file.
4. Write a short plan (files, public signatures, test list mapped to R-numbers) and show it. If the task
   is ambiguous or conflicts with interfaces.md/invariants.md, ask before coding; if it hits an open
   decision (Dx in docs/tasks/README.md), use the listed default and note it.
5. Write the tests from the "Tests" table first. Run them and show that they fail for the right reason
   (missing code, not import errors in the test itself).
6. Implement in small steps, running the relevant test file after each step. Keep public contracts as in
   docs/design/interfaces.md.
7. Run `make check` (and `make check-all` if the task has integration tests). Paste the summary lines.
8. Use the task-reviewer subagent on the diff. For core packages also use the test-critic subagent.
   Fix BLOCKER and SHOULD-FIX items that affect correctness or requirements; ignore pure preferences.
9. Update docs if contracts or invariants changed. Set Status to `review` in the task file and in
   docs/tasks/README.md. Add follow-ups you discovered to the task's Notes.
10. Commit with a Conventional Commit message and `-s` (DCO). Summarize: what was built, test evidence,
    reviewer findings and how they were handled, open questions. Do not push.
