#!/usr/bin/env bash
# Entrypoint of the ebs-postgres image. EBS_PG_ROLE selects what the container runs:
#   primary    the official postgres entrypoint (initdb scripts on first start)
#   standby    pg_basebackup from $EBS_PG_PRIMARY_HOST on first start, then a hot standby
#   pgbouncer  PgBouncer in front of $EBS_PG_PRIMARY_HOST
# `ebs-entrypoint.sh switch-primary HOST` (inside a running pgbouncer container) points the
# pool at HOST and reloads PgBouncer; see deploy/postgres/README.md § Failover.
set -euo pipefail

PGBOUNCER_RUN=/run/pgbouncer

log() { printf 'ebs-entrypoint: %s\n' "$*" >&2; }

die() {
    log "error: $*"
    exit 1
}

check_host() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || die "invalid host name '$1'"
}

# Server keys must be owned by the server user with mode 0600; bind mounts keep host owners.
install_tls() {
    local dest=$1 f
    install -d -o postgres -g postgres -m 0700 "$dest"
    for f in ca.crt server.crt server.key; do
        [[ -r "/certs/$f" ]] || die "/certs/$f missing: run deploy/postgres/scripts/gen-certs.sh"
        install -o postgres -g postgres -m 0600 "/certs/$f" "$dest/$f"
    done
}

# pgpass fields escape '\' and ':' with a backslash.
pgpass_escape() {
    local s=${1//\\/\\\\}
    printf '%s' "${s//:/\\:}"
}

# PgBouncer userlist fields are double-quoted; a literal '"' is doubled.
userlist_entry() {
    printf '"%s" "%s"\n' "${1//\"/\"\"}" "${2//\"/\"\"}"
}

prepare_server() {
    install_tls /var/lib/postgresql/tls
    chown postgres:postgres /var/lib/pgbackrest /var/log/pgbackrest
}

run_standby() {
    : "${REPLICATION_PASSWORD:?REPLICATION_PASSWORD must be set}"
    local primary=${EBS_PG_PRIMARY_HOST:-primary}
    check_host "$primary"
    prepare_server
    # Recreated on every start from the environment; the walreceiver reads it too.
    local pgpass=/var/lib/postgresql/.pgpass
    printf '*:*:*:replicator:%s\n' "$(pgpass_escape "$REPLICATION_PASSWORD")" >"$pgpass"
    chown postgres:postgres "$pgpass"
    chmod 0600 "$pgpass"

    # Written only after pg_basebackup succeeds, so a clone interrupted half-way is redone
    # (PG_VERSION alone could be left behind). It stays after a promotion.
    local marker=$PGDATA/ebs-clone-complete
    if [[ ! -f "$marker" ]]; then
        install -d -o postgres -g postgres -m 0700 "$PGDATA"
        find "$PGDATA" -mindepth 1 -delete
        log "waiting for $primary to accept connections"
        until gosu postgres pg_isready -q -h "$primary" -p 5432; do sleep 2; done
        # The slot is missing after a point-in-time restore of the primary (pgBackRest does not
        # back up pg_replslot): let pg_basebackup create it then.
        local slot_args=(--slot=standby1)
        if [[ -z "$(gosu postgres psql -X -At --no-password \
            "host=$primary port=5432 dbname=postgres user=replicator sslmode=require" \
            -c "SELECT 1 FROM pg_replication_slots WHERE slot_name = 'standby1'")" ]]; then
            log "replication slot standby1 missing on $primary: creating it"
            slot_args+=(--create-slot)
        fi
        log "cloning $primary into $PGDATA"
        gosu postgres pg_basebackup \
            --dbname="host=$primary port=5432 user=replicator sslmode=require application_name=standby1" \
            --pgdata="$PGDATA" --wal-method=stream "${slot_args[@]}" --write-recovery-conf \
            --checkpoint=fast --no-password
        gosu postgres touch "$marker"
    fi
    exec docker-entrypoint.sh "$@"
}

run_pgbouncer() {
    : "${EBS_PASSWORD:?EBS_PASSWORD must be set}"
    : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}"
    local primary=${EBS_PG_PRIMARY_HOST:-primary}
    check_host "$primary"
    install -d -o postgres -g postgres -m 0700 "$PGBOUNCER_RUN"
    install_tls "$PGBOUNCER_RUN/tls"
    sed "s/@EBS_PG_PRIMARY_HOST@/$primary/" /etc/pgbouncer/pgbouncer.ini >"$PGBOUNCER_RUN/pgbouncer.ini"
    {
        userlist_entry ebs "$EBS_PASSWORD"
        userlist_entry postgres "$POSTGRES_PASSWORD"
    } >"$PGBOUNCER_RUN/userlist.txt"
    chown postgres:postgres "$PGBOUNCER_RUN/pgbouncer.ini" "$PGBOUNCER_RUN/userlist.txt"
    chmod 0600 "$PGBOUNCER_RUN/pgbouncer.ini" "$PGBOUNCER_RUN/userlist.txt"
    exec gosu postgres pgbouncer "$PGBOUNCER_RUN/pgbouncer.ini"
}

switch_primary() {
    local host=${1:?usage: ebs-entrypoint.sh switch-primary HOST}
    check_host "$host"
    local ini=$PGBOUNCER_RUN/pgbouncer.ini
    [[ -f "$ini" ]] || die "$ini not found: run this inside the pgbouncer container"
    sed -i -E "s/^(ebs = host=)[^ ]+/\1$host/" "$ini"
    grep -q "^ebs = host=$host " "$ini" || die "could not rewrite the ebs entry in $ini"
    # PgBouncer runs as PID 1 of its container; SIGHUP reloads the config.
    kill -HUP 1
    log "pgbouncer now points at $host (reloaded)"
}

case "${1:-}" in
switch-primary)
    shift
    switch_primary "$@"
    exit 0
    ;;
esac

case "${EBS_PG_ROLE:-primary}" in
primary)
    prepare_server
    exec docker-entrypoint.sh "$@"
    ;;
standby) run_standby "$@" ;;
pgbouncer) run_pgbouncer ;;
*) die "unknown EBS_PG_ROLE '${EBS_PG_ROLE}' (primary, standby or pgbouncer)" ;;
esac
