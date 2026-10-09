#!/usr/bin/env bash
# deploy-smoke (P0-11 R1, R2, R4): bring up primary + standby + PgBouncer, check pooling, TLS,
# streaming replication, then fail over to the standby and reconnect through PgBouncer.
# Needs Docker (or COMPOSE="podman compose") and openssl. Takes a few minutes on first build.
set -euo pipefail
# shellcheck source=tests/deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

smoke_setup compose

step "build and start the stack"
compose up -d --build
wait_for 180 "primary accepting connections" server_ready primary
wait_for 180 "standby accepting connections" server_ready standby
wait_for 60 "pgbouncer serving the ebs database" sql_pool "SELECT 1"

step "R1: clients go through PgBouncer in transaction pooling mode"
pool_mode=$(compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" pgbouncer \
    psql -X -At "host=127.0.0.1 port=6432 dbname=pgbouncer user=postgres sslmode=require" \
    -c "SHOW CONFIG" | awk -F'|' '$1 == "pool_mode" { print $2 }')
expect_eq "$pool_mode" "transaction" "pgbouncer pool_mode"
expect_eq "$(sql_pool "SHOW statement_timeout")" "30s" "ebs role statement_timeout (R6)"
expect_eq "$(sql_super primary postgres "SHOW max_connections")" "100" "max_connections (R6)"
expect_eq "$(sql_super primary postgres "SHOW idle_in_transaction_session_timeout")" "1min" \
    "idle_in_transaction_session_timeout (R6)"

step "R2: PgBouncer -> server connections use TLS; plain-text TCP is rejected"
expect_eq "$(sql_pool "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")" "t" \
    "server-side TLS"
# expect_plaintext_refused HOST PORT PATTERN: a sslmode=disable login must fail with PATTERN
# in the error (so a DNS or password failure doesn't count as a pass).
expect_plaintext_refused() {
    local out
    if out=$(compose exec -T -e PGPASSWORD="$EBS_PASSWORD" pgbouncer \
        psql -X -At "host=$1 port=$2 dbname=ebs user=ebs sslmode=disable" -c "SELECT 1" 2>&1); then
        fail "$1:$2 accepted a non-TLS connection"
    fi
    grep -Eqi "$3" <<<"$out" || fail "$1:$2 refused plain text for another reason: $out"
    echo "ok: non-TLS connection to $1:$2 refused"
}
expect_plaintext_refused primary 5432 "no encryption"
expect_plaintext_refused 127.0.0.1 6432 "ssl|tls"

step "R1: the standby streams from the primary"
# The standby accepts connections before its walreceiver has caught up: poll.
streaming() {
    [[ "$(sql_super primary postgres \
        "SELECT application_name || ':' || state FROM pg_stat_replication")" == standby1:streaming ]]
}
wait_for 60 "pg_stat_replication shows standby1:streaming" streaming
expect_eq "$(sql_super standby postgres "SELECT pg_is_in_recovery()")" "t" "standby in recovery"
sql_pool "CREATE TABLE smoke (id int PRIMARY KEY, note text)" >/dev/null
sql_pool "INSERT INTO smoke VALUES (1, 'before failover')" >/dev/null
standby_has_row() { [[ "$(sql_super standby ebs "SELECT count(*) FROM smoke")" == 1 ]]; }
wait_for 60 "row replicated to the standby" standby_has_row

step "R4: fail over — stop the primary, promote the standby, switch PgBouncer"
compose stop primary
sql_super standby postgres "SELECT pg_promote(wait => true, wait_seconds => 60)" >/dev/null
expect_eq "$(sql_super standby postgres "SELECT pg_is_in_recovery()")" "f" "standby promoted"
"$PG_DIR/scripts/pgbouncer-switch.sh" standby
writable_via_pool() { sql_pool "INSERT INTO smoke VALUES (2, 'after failover')"; }
wait_for 60 "write through pgbouncer to the new primary" writable_via_pool
expect_eq "$(sql_pool "SELECT string_agg(id::text, ',' ORDER BY id) FROM smoke")" "1,2" \
    "rows after failover"
expect_eq "$(sql_pool "SELECT pg_is_in_recovery()")" "f" "pgbouncer backend is the new primary"

step "PASS test_compose.sh"
