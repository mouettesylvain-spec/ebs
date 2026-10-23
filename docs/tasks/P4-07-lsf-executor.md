# P4-07 — LSF executor

Status: todo · Phase: 4 · Depends on: P1-02, P1-03, P1-05 · Parallel-safe with: P4-01…P4-06 · Size: L

## Goal
An `LsfExecutor` (`bsub`/`bjobs -json`/`bkill`) behind the same executor API as SLURM, with a `FakeLsf` and the
shared executor contract suite, so sites running LSF can use EBS.

## Read first
- docs/architecture.md § "SLURM execution…" (submission strategy, status polling; "the executor interface leaves
  room for LSF")
- docs/design/interfaces.md §8 (executors); docs/tasks/P1-02-slurm-executor.md, P1-03-licenses-accounts.md
- Existing code to follow: `src/ebs/exec/slurm/{cli,executor,arrays,licenses,states,jobscript}.py`

## Scope (files)
- create `src/ebs/exec/lsf/{__init__,cli,executor,arrays,licenses,states,jobscript}.py`
- create `tests/fakes/lsf.py` (FakeLsf), `tests/unit/exec/lsf/*`, extend the executor contract parametrization
  in `tests/unit/exec/` to include LSF

## Out of scope
Pilot pool on LSF (P3-06 owns the SLURM one), license-server sync for LSF sites, Kubernetes. Changing the
executor API (note "CONTRACT CHANGE" and update interfaces.md if unavoidable).

## Requirements
- R1 Submission: `bsub` argv built as a list (no shell), with queue, project, `-n`, memory (`rusage[mem=…]`),
  walltime (`-W`), job name, and output paths; job arrays `name[1-N]%throttle` for regressions.
- R2 Status: one `bjobs -json -a` call per build (adaptive polling), mapped to the executor state set; pending
  reasons separate "licenses" from "resources".
- R3 Licenses: declared features map to `rusage[<feature>=<n>]`; the mapping is config, no server names in repo.
- R4 Cancel via `bkill`; a duplicate submission after a lost `bsub` reply is detected and cancelled (as P1-02).
- R5 Failures: exit codes, OOM (`TERM_MEMLIMIT`), run-limit and host failures map to the INFRA classes of P1-05;
  transient `bsub`/`bjobs` errors are retried with backoff.
- R6 Limits (`max_pending_jobs`, array size, submit rate) come from `[lsf]` config like D5's `[slurm]` keys.
- R7 The executor contract suite passes for `LocalExecutor`, `SlurmExecutor` and `LsfExecutor` unchanged.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_cli.py::test_bsub_argv_golden`, `test_arrays.py::test_array_spec_throttle` | R1 | unit |
| `test_states.py::test_bjobs_json_mapping`, `::test_pending_reason_split` | R2 | unit |
| `test_licenses.py::test_rusage_mapping` | R3 | unit |
| `test_executor.py::test_cancel`, `::test_lost_reply_duplicate_cancelled` | R4 | unit |
| `test_states.py::test_infra_classification[oom,runlimit,host]`, `test_cli.py::test_transient_retry` | R5 | unit |
| `test_config.py::test_limits_from_config` | R6 | unit |
| `tests/unit/exec/test_contract.py[lsf]` (FakeLsf) | R7 | unit |

## Done when
- [ ] `make check` passes (paste the summary line)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] interfaces.md updated if the executor contract changed
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
No real queue names or hostnames in fixtures. A real-LSF nightly (`grid`-style marker) is a follow-up once a site
volunteers a cluster; do not add it here.
