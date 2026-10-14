#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# EBS node scratch cleanup: a SLURM node Epilog (slurm.conf `Epilog=`, run by slurmd when a job
# ends). It removes the ebs-runner scratch of the job that just ended, which the runner leaves
# behind only when it is killed without warning (SIGKILL, OOM killer, node trouble): on success,
# failure, timeout and SIGTERM the runner removes its own scratch (invariant I16).
#
# Each runner action dir is `<scratch base>/<build>/<action>/` and holds `.ebs-job`, the SLURM
# job id. This script removes the dirs whose marker names this job and is owned by the job's
# user, then the build dirs left empty. Deletion runs as the job's user (setpriv), so it can
# never remove more than that user could. See README.md for installation and configuration.
#
# An epilog that fails drains the node, so this script always exits 0.

set -u
shopt -s nullglob

conf=${EBS_EPILOG_CONF:-/etc/ebs/epilog.conf}
if [[ -r $conf ]]; then
    # shellcheck source=/dev/null
    source "$conf"
fi
# Space-separated glob patterns of scratch bases: ebs.toml `[scratch].dir` as seen on the node.
scratch_globs=${EBS_SCRATCH_GLOBS:-/tmp/ebs}

job=${SLURM_JOB_ID:-}
uid=${SLURM_JOB_UID:-}
gid=${SLURM_JOB_GID:-}
[[ $job =~ ^[0-9]+$ && $uid =~ ^[0-9]+$ ]] || exit 0
[[ $gid =~ ^[0-9]+$ ]] || gid=$(id -g "$uid" 2>/dev/null) || exit 0

# EBS jobs carry a comment starting with their build UUID. When slurmd passes a non-empty
# comment that does not look like that, this is not an EBS job: nothing to do. Otherwise (no
# or empty comment) the markers alone decide; only ebs-runner ever writes them.
uuid_re='^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}([^0-9A-Fa-f-]|$)'
if [[ -n ${SLURM_JOB_COMMENT:-} && ! ${SLURM_JOB_COMMENT} =~ $uuid_re ]]; then
    exit 0
fi

log() {
    if command -v logger >/dev/null 2>&1; then
        logger -t ebs-epilog -- "job $job: $*"
    fi
}

# Run "$@" as the job's user: as root via setpriv; otherwise only if we are that user.
as_user() {
    if [[ $EUID -eq $uid ]]; then
        "$@"
    elif [[ $EUID -eq 0 ]] && command -v setpriv >/dev/null 2>&1; then
        setpriv --reuid="$uid" --regid="$gid" --clear-groups -- "$@"
    else
        return 1
    fi
}

remove_dir() {
    # Tools leave read-only dirs behind; the owner may make them writable again.
    as_user find "$1" -depth -type d ! -perm -u=rwx -exec chmod u+rwx {} + 2>/dev/null
    as_user rm -rf --one-file-system -- "$1"
}

read -r -a patterns <<<"$scratch_globs"
for pattern in "${patterns[@]}"; do
    # The pattern is a glob on purpose (e.g. /local/*/ebs).
    # shellcheck disable=SC2206
    bases=($pattern)
    for base in "${bases[@]}"; do
        [[ -d $base && ! -L $base ]] || continue
        for marker in "$base"/*/*/.ebs-job; do
            dir=${marker%/.ebs-job}
            build=${dir%/*}
            [[ ! -L $build && ! -L $dir && -f $marker && ! -L $marker ]] || continue
            [[ $(stat -c %u -- "$marker" 2>/dev/null) == "$uid" ]] || continue
            # Read as the user, bounded: a FIFO swapped in must not hang the node's epilog.
            owner_job=$(as_user timeout 5 head -c 32 -- "$marker" 2>/dev/null)
            owner_job=${owner_job%%$'\n'*}
            [[ $owner_job == "$job" ]] || continue
            if remove_dir "$dir"; then
                log "removed scratch $dir"
            else
                log "could not remove scratch $dir"
            fi
            as_user rmdir -- "$build" 2>/dev/null  # only once empty
        done
    done
done
exit 0
