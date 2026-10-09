# shellcheck shell=bash
# Shared helpers for the deploy-smoke scripts (sourced, not executed).
#
# Each script runs its own throw-away compose project with random passwords and fresh
# self-signed certificates in a temp dir, and removes containers and volumes on exit.
# COMPOSE overrides the compose command (default "docker compose"; e.g. "podman compose").

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PG_DIR=$REPO_ROOT/deploy/postgres

smoke_setup() {
    local name=$1
    WORK=$(mktemp -d)
    PROJECT="ebs-${name}-$$"
    POSTGRES_PASSWORD=$(openssl rand -hex 16)
    EBS_PASSWORD=$(openssl rand -hex 16)
    REPLICATION_PASSWORD=$(openssl rand -hex 16)
    PGBACKREST_REPO1_CIPHER_PASS=$(openssl rand -hex 24)
    "$PG_DIR/scripts/gen-certs.sh" "$WORK/certs" >/dev/null 2>&1
    {
        echo "POSTGRES_PASSWORD=$POSTGRES_PASSWORD"
        echo "EBS_PASSWORD=$EBS_PASSWORD"
        echo "REPLICATION_PASSWORD=$REPLICATION_PASSWORD"
        echo "PGBACKREST_REPO1_CIPHER_PASS=$PGBACKREST_REPO1_CIPHER_PASS"
        echo "EBS_PG_CERT_DIR=$WORK/certs"
        # Per-run port so the two scripts can run side by side.
        echo "EBS_PGBOUNCER_PORT=${EBS_PGBOUNCER_PORT:-$((20000 + $$ % 20000))}"
    } >"$WORK/env"
    chmod 0600 "$WORK/env"
    read -r -a COMPOSE_CMD <<<"${COMPOSE:-docker compose}"
    COMPOSE_CMD+=(-p "$PROJECT" --env-file "$WORK/env" -f "$PG_DIR/compose.yaml")
    export EBS_COMPOSE="${COMPOSE_CMD[*]}"
    trap smoke_teardown EXIT
}

smoke_teardown() {
    local status=$?
    if [[ $status -ne 0 ]]; then
        echo "--- FAILED (exit $status); container logs follow ---" >&2
        compose logs --no-color --tail=80 >&2 || true
    fi
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
    exit "$status"
}

compose() { "${COMPOSE_CMD[@]}" "$@"; }

step() { printf '\n== %s\n' "$*"; }

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Run SQL as the postgres superuser over the server's socket (peer auth).
sql_super() {
    local service=$1 db=$2 query=$3
    compose exec -T -u postgres "$service" psql -X -v ON_ERROR_STOP=1 -At -d "$db" -c "$query"
}

# Run SQL as the ebs role through PgBouncer (TLS + SCRAM), from inside the pgbouncer container.
sql_pool() {
    local query=$1
    compose exec -T -e PGPASSWORD="$EBS_PASSWORD" pgbouncer \
        psql -X -v ON_ERROR_STOP=1 -At \
        "host=127.0.0.1 port=6432 dbname=ebs user=ebs sslmode=require" -c "$query"
}

# wait_for TIMEOUT_S DESCRIPTION CMD... : retry CMD every 2 s until it succeeds.
wait_for() {
    local timeout=$1 what=$2 deadline
    shift 2
    deadline=$((SECONDS + timeout))
    until "$@" >/dev/null 2>&1; do
        ((SECONDS < deadline)) || fail "timed out after ${timeout}s waiting for $what"
        sleep 2
    done
    echo "ok: $what"
}

server_ready() { compose exec -T "$1" pg_isready -q -h 127.0.0.1 -p 5432 -U postgres; }

# expect_eq ACTUAL EXPECTED DESCRIPTION
expect_eq() {
    [[ "$1" == "$2" ]] || fail "$3: expected '$2', got '$1'"
    echo "ok: $3 = $1"
}
