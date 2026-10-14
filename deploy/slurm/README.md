# SLURM node setup for EBS

## Scratch cleanup epilog (`epilog.d/ebs-scratch-cleanup.sh`)

`ebs-runner` executes each action in node-local scratch, `<[scratch].dir>/<build>/<action>/`,
and removes it itself on success, failure, timeout and SIGTERM (`scancel`, preemption, time
limit: the runner has SLURM's `KillWait` to finish). A job killed outright (SIGKILL after
`KillWait`, the OOM killer, a crashed runner) cannot clean up, so the node epilog does it: the
runner writes the job id into `<action>/.ebs-job` before staging anything, and the epilog
removes every action dir whose marker names the ending job and belongs to the job's user, then
the build dirs left empty.

Safety:

- Deletion runs as the job's user (`setpriv --reuid --regid --clear-groups`), so the epilog can
  never remove anything that user could not remove.
- Symlinked base, build or action dirs are never followed; markers owned by another user are
  ignored.
- When slurmd passes a non-empty `SLURM_JOB_COMMENT` that does not start with a build UUID,
  the job is skipped at once (the EBS SLURM executor is to set `--comment=<build uuid> …`,
  P1-02). With no or an empty comment, the markers alone decide.
- The script always exits 0: a failing epilog would drain the node.

### Install

On every compute node (the Ansible role in `deploy/ansible` can do this):

```sh
install -o root -g root -m 0755 deploy/slurm/epilog.d/ebs-scratch-cleanup.sh \
    /etc/slurm/epilog.d/ebs-scratch-cleanup.sh
```

and in `slurm.conf` (Slurm runs every file of a glob in order):

```
Epilog=/etc/slurm/epilog.d/*
```

Requirements: bash, coreutils, `find`, util-linux `setpriv` (RHEL 8+ and Debian ship it).

### Configure

`/etc/ebs/epilog.conf` (optional, sourced by bash; `EBS_EPILOG_CONF` names another file):

```sh
# Space-separated glob patterns: the [scratch].dir of ebs.toml as it expands on the nodes.
# Default: /tmp/ebs. With a per-job TMPDIR, use a glob, e.g. "/local/*/ebs".
EBS_SCRATCH_GLOBS="/local/scratch/ebs"
```

### Check

```sh
sudo env SLURM_JOB_ID=123 SLURM_JOB_UID=$(id -u alice) SLURM_JOB_GID=$(id -g alice) \
    /etc/slurm/epilog.d/ebs-scratch-cleanup.sh
journalctl -t ebs-epilog     # one line per removed scratch dir
```

Tests: `tests/unit/deploy/test_epilog.py` (also runs `shellcheck` when it is installed) and
`tests/integration/test_runner_cleanup.py` (FakeSlurm kills a runner with SIGKILL; the epilog
cleans up).

## Debug area

Runners write debug files to `[debug].root`, `/debug/<domain>/<build>/<action>/`: `live.log`
while the tool runs (removed after a pass unless `debug.on_success`), and on failure
`tool.log`, `command.json` and the step's `debug.collect` files, capped at `debug.max_size`
(default `[debug].max_size`, 2 GiB). An admin creates each domain dir, group `<domain>`, mode
2750; the runners create everything below it in that group (dirs 2750, files 0640). Without the
domain dir nothing is written and the runner prints a warning; the action's result is not
affected.
