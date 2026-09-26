# P1-03 — Licenses, accounts/QOS, constraints

Status: todo · Phase: 1 · Depends on: P1-02 · Size: S

## Goal
Jobs start only when their EDA licenses are available, run under the right team account and QOS, and land
on nodes with the right OS image.

## Read first
- docs/architecture.md § "SLURM execution…" → Licenses bullets; § "Roadmap and tech stack" → Grid bullet
  (`RemoteFuzzyMatch`)

## Scope (files)
- create `src/ebs/exec/slurm/licenses.py`, `accounts.py`
- extend `[slurm]` config: `license_server` (default `flexlm`), `account_map` (project ⇒ account),
  `qos` (`default`, `ci`, `release`), `os_constraint`
- tests under `tests/unit/exec/slurm/`

## Requirements
- R1 License requests merge toolchain + step declarations (step overrides per feature) and render as
  `-L feat@server:count,…` sorted; with `license_fuzzy = true` render `feat:count` (RemoteFuzzyMatch).
- R2 Feature names are validated (`^[A-Za-z0-9_.-]+$`); counts positive ints; unknown features are allowed
  (SLURM rejects them) but the SLURM error is surfaced with the feature name.
- R3 Account = `account_map[flow.project]`, else error listing known projects; QOS from build kind:
  `ci` builds (`--ci`) ⇒ `qos.ci`, release builds ⇒ `qos.release`, else `qos.default`.
- R4 `--constraint` from config `os_constraint` (key excludes node OS; this enforces it instead); steps may
  add constraints (ANDed).
- R5 Neither licenses, account, QOS nor constraint affect the action key (extend I3's excluded-field list
  and its test).
- R6 `ebs status` shows `pending: licenses` using P1-01 reason mapping (FakeSlurm license pool test).

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_licenses.py::test_merge_and_render[...]`, `::test_fuzzy` | R1 | unit |
| `test_licenses.py::test_validation[...]`, `::test_unknown_feature_error_surface` | R2 | unit |
| `test_accounts.py::test_mapping_and_qos[...]` | R3 | unit |
| `test_accounts.py::test_constraints` | R4 | unit |
| `tests/unit/plan/test_key_sensitivity.py` (extended excluded fields) | R5 | property |
| `tests/integration/slurm/test_pending_licenses.py` | R6 | integration |

## Done when
- [ ] `make check-all` passes
