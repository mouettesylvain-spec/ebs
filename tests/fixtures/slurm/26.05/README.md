# SLURM 26.05 `--json` fixtures

Shape of `squeue --json` / `sacct --json` (data_parser v0.0.44) as used by
`ebs.exec.slurm.models`. Anonymized: users `u1…`, groups `g1…`, accounts `a1…`, partitions `p1…`,
QOS `q1…`, cluster `c1`, nodes `n001…`, license server `flexlm`, paths under `/work/u1`.

These files were written by hand from the SLURM 26.05 JSON schema (no cluster was reachable when
P1-01 was implemented). Replace them with real captures as soon as one is: run
`scripts/capture_slurm_fixtures.py` on a login node (see its `--help`), review the diff for
anything not anonymized, and keep the file names so `tests/unit/exec/slurm/test_cli.py` picks
them up. The expectations in `test_parse_fixtures` describe each file's jobs.
