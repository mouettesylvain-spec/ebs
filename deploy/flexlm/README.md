# FlexLM → SLURM `LastConsumed` sync (`ebs-flexlm-sync`)

EBS jobs request licenses as SLURM remote licenses (`sbatch -L feature@flexlm:1`, P1-03), so a
job starts only when SLURM thinks the license is free. Licenses checked out *outside* SLURM
(interactive GUI sessions, other clusters) are invisible to it, and a job could then start and
block on FlexLM. Since Slurm 23.02 a remote license has a `LastConsumed` count: the licenses in
use on the license server, by anyone. SLURM reserves `LastConsumed` minus what its own jobs hold.

`ebs-flexlm-sync` keeps that count current. Every minute it runs `lmutil lmstat -a -c <license>`
for each configured server, reads "Total of N licenses in use" of each configured feature, and
runs `sacctmgr -i update resource <feature> server=<server> set LastConsumed=<N>` when SLURM's
value differs. Feature names match lmstat and SLURM without regard to case.

## Install

On **one** admin host with the SLURM client, MUNGE and `lmutil` (the slurmdbd host is a good
choice), not on login or compute nodes:

```sh
useradd --system --no-create-home ebs-flexlm
sacctmgr -i add user ebs-flexlm account=root adminlevel=operator   # may update resources
install -m 644 deploy/flexlm/ebs-flexlm-sync.service deploy/flexlm/ebs-flexlm-sync.timer \
    /etc/systemd/system/
install -m 640 -g ebs-flexlm deploy/flexlm/flexlm.toml.example /etc/ebs/flexlm.toml  # edit it
systemctl daemon-reload && systemctl enable --now ebs-flexlm-sync.timer
```

Every configured feature must exist as a remote license first, e.g.
`sacctmgr add resource simfeat server=flexlm servertype=flexlm count=10 cluster=<c> percentallowed=100`.

Check by hand before enabling the timer: `sudo -u ebs-flexlm env EBS_CONFIG=/etc/ebs/flexlm.toml
/opt/ebs/bin/ebs-flexlm-sync --dry-run` prints the `sacctmgr` commands it would run.

## Behaviour and alerts

Logs are JSON lines in the journal (`journalctl -u ebs-flexlm-sync`).

| Situation | What happens | Event | Exit |
| --- | --- | --- | --- |
| Usage changed | `sacctmgr update` | `flexlm_last_consumed` (info) | 0 |
| Usage unchanged | nothing | — | 0 |
| License server or **any** of its vendor daemons down, `lmstat` failed, timed out or truncated | no update for that server | `flexlm_server_unavailable` (error) | 1 |
| `sacctmgr show resource` failed (slurmdbd down) | no update for that server | `flexlm_slurm_unavailable` (error) | 1 |
| Configured feature absent from `lmstat` output | that feature left unchanged | `flexlm_feature_unreported` (error) | 1 |
| `sacctmgr update` failed | — | `flexlm_update_failed` (error) | 1 |
| Configured feature has no SLURM resource | skipped | `flexlm_feature_not_in_slurm` (warning, **once** until it exists) | 0 |

A non-zero exit fails the unit, so hook your alerting on it (`OnFailure=` or your monitoring
of failed systemd units). When a server is down, `LastConsumed` keeps its last value: stale
but safe for a few minutes. Fix the server, or set it by hand if the outage lasts.

The tool refuses to run inside a SLURM job (`SLURM_JOB_ID` set): it is never part of a build.
