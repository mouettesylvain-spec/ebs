---
paths:
  - "src/ebs/exec/**"
  - "src/ebs/runner/**"
  - "src/ebs/sandbox/**"
  - "deploy/slurm/**"
---
# Rules for executors, runner and sandbox

- Test against FakeSlurm (`tests/fakes/slurm`) — never call real `sbatch` outside `tests/grid/`.
- Build argv lists; never pass user-controlled strings through a shell. Job scripts come from a fixed
  template with `shlex.quote`d values only.
- One `squeue`/`sacct` call per poll for all jobs of a build. No per-job polling loops.
- The runner must stay importable with the minimal dependency set (see docs/design/overview.md layering);
  don't import driver, planner or service code from it.
- Every code path that creates node scratch must have a cleanup test covering success, failure, timeout
  and SIGTERM (invariant I16).
- Resource requests, licenses, accounts, QOS, constraints and sandbox choice must never enter the action key.
