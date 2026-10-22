# EBS revalidation (P3-05)

`ebs release revalidate NAME VERSION` re-runs a release from its sign-off bundle
(`ebs release archive`) on newer tools, compares every action with its archived result and
attaches the report (JSON and Markdown, in the domain's CAS) to the release. `ebs release
revalidate --all --domain D` does that for every archived release of D that is not yanked; the
timer here runs it quarterly, one instance per domain.

- `--toolchain-override questa=latest` (or `[revalidate].toolchain_overrides`) runs the flow's
  `questa` toolchain on the most recently registered current `questa/*` module (registration
  order, not version order: re-registering an older version makes it "latest");
  `questa=questa/2026.1` on that module. The others keep the archived toolchain records. A
  config override is skipped for releases that do not use that toolchain.
- With `--all`, a release that cannot be revalidated (bundle missing or altered) is reported as
  a warning and the other releases still run.
- The re-run is a normal build on the grid (`--executor slurm`) that reads the action cache but
  never writes it; `--no-cache` re-runs every action.
- Comparators: `sim` (per test and seed: pass/fail equal, coverage within
  `coverage_tolerance` points) and `lint` (violation sets after normalization). Steps without one
  are compared by status and output digests.
- Verdict: `reproduced`, `differences` (an action changed, or its comparator could not compare
  it) or `failed` (the re-run did not complete). `ebs release revalidate` exits 1 unless every
  release reproduced; the unit treats that as success, since the report says why.
- `[revalidate.notify]` sends the Markdown report to a webhook (JSON POST with `text`, `verdict`,
  `release`, `domain`, `report`) and/or by email; `when = "problems"` skips reproduced runs.
  A failed delivery is logged and never fails the run.

## Install (one timer per domain)

1. Create the `ebs-revalidate` account: a member of each domain's Unix group, allowed to submit
   SLURM jobs, able to read the cold-storage directory of the bundles, with `release_write` on
   the domain in the metadata service (it attaches the reports).
2. Register the new toolchain versions (`ebs toolchain register questa/2026.1`) before the run.
3. `install -m 644 ebs-revalidate@.service ebs-revalidate@.timer /etc/systemd/system/`
4. `install -m 640 -g ebs-revalidate revalidate.toml.example /etc/ebs/revalidate.toml` and edit it.
5. `systemctl daemon-reload && systemctl enable --now ebs-revalidate@<domain>.timer`.

Reports show in the dashboard's release list; `ebs release show` names the release, and the
report objects stay in the CAS for as long as the release (they are GC roots).
