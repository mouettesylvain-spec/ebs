# P3-08 — FlexLM `LastConsumed` sync

Status: review · Phase: 3 · Depends on: P1-03 · Size: S

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

## Notes
- Package `ebs.admin` (L4, independent of `driver` via `|` in .importlinter): admin tools that
  run outside builds. No new dependency.
- Current `LastConsumed` is read with `sacctmgr -n -P show resource server=S
  format=Name,Server,LastConsumed` (Slurm >= 23.02); an empty value counts as changed.
- "Warn once" persists across the one-minute oneshot runs in `[flexlm].state_file`
  (systemd `StateDirectory=ebs-flexlm`); an outage keeps the memory.
- Any vendor daemon down skips the whole server (safe; per-daemon handling is a possible
  follow-up). Feature names match lmstat and SLURM case-insensitively.
- Follow-ups: verify the `sacctmgr show resource format=…LastConsumed` field name and the
  exact lmstat down messages on the site's Slurm / FlexLM versions (grid check, nightly);
  servers run sequentially with a 10 s per-call timeout under the unit's 50 s budget — run them
  in parallel if a site has more than ~2 license servers.
