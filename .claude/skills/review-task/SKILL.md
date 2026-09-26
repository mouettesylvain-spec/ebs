---
name: review-task
description: Independent review of a task branch before a human merges it. Use when the user runs /review-task <ID>.
disable-model-invocation: true
---
Review task $ARGUMENTS without modifying code.

1. Run `make check` and, if integration tests exist for the task, `make check-all`. Record results.
2. Use the task-reviewer subagent with task ID $ARGUMENTS.
3. If the task touches src/ebs/{core,plan,sources,cas,gc,driver}, also use the test-critic subagent.
4. Produce a merge recommendation: APPROVE, APPROVE WITH FOLLOW-UPS (list them as new task notes), or
   CHANGES REQUESTED (list blockers), with the evidence from steps 1–3.
