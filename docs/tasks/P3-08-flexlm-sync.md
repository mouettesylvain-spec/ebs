# P3-08 — FlexLM `LastConsumed` sync

Status: todo · Phase: 3 · Depends on: P1-03 · Size: S

## Goal
SLURM accounts for licenses consumed outside it (interactive GUI sessions) so jobs never start and then
block on FlexLM.

## Read first
- docs/architecture.md § "SLURM execution…" → Licenses (remote licenses, LastConsumed); SLURM licenses docs

## Scope (files)
- create `src/ebs/admin/flexlm_sync.py` (entry point `ebs-flexlm-sync`), `deploy/flexlm/*.timer|service`,
  tests with fixture `lmstat -a` outputs (synthetic)

## Requirements
- R1 Parses `lmutil lmstat -a -c <server>` output for configured features: total issued, total in use.
- R2 Computes `LastConsumed` = in use (by anyone) and updates via `sacctmgr -i update resource <feat> server=<srv>
  set LastConsumed=<n>` — only when changed; dry-run mode prints commands.
- R3 Robust to server down / partial output (no update, alert log) and to feature names absent in SLURM (warn once).
- R4 Runs every minute via timer as an admin account; never runs inside user builds.

## Tests
`tests/unit/admin/test_flexlm_parse.py` (R1, fixtures), `test_flexlm_update.py` (R2, fake sacctmgr),
`test_flexlm_errors.py` (R3), `tests/unit/deploy/test_flexlm_units.py` (R4)
