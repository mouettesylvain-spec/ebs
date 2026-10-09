#!/usr/bin/env bash
# deploy-smoke (P0-11 R3): pgBackRest full backup + WAL archiving + point-in-time recovery.
# Writes rows, takes a full backup, writes more rows, notes a time T, writes more rows, restores
# the primary to T and checks that exactly the rows committed before T are back.
# Needs Docker (or COMPOSE="podman compose") and openssl.
set -euo pipefail
# shellcheck source=tests/deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

smoke_setup backup

backrest() { compose exec -T -u postgres primary pgbackrest --stanza=ebs "$@"; }

step "start the primary"
compose up -d --build primary
wait_for 180 "primary accepting connections" server_ready primary

step "archiving works (pgbackrest check forces a WAL switch and waits for the archive)"
backrest check

step "rows 1-3, full backup"
sql_super primary ebs "CREATE TABLE pitr (id int PRIMARY KEY)" >/dev/null
sql_super primary ebs "INSERT INTO pitr SELECT generate_series(1, 3)" >/dev/null
backrest --type=full backup
backrest info

step "rows 4-6 after the backup, then the target time, then rows 7-9"
sql_super primary ebs "INSERT INTO pitr SELECT generate_series(4, 6)" >/dev/null
target=$(sql_super primary postgres "SELECT clock_timestamp()")
sleep 2
sql_super primary ebs "INSERT INTO pitr SELECT generate_series(7, 9)" >/dev/null
expect_eq "$(sql_super primary ebs "SELECT count(*) FROM pitr")" "9" "rows before restore"
# Make sure the WAL holding rows 7-9 (past the target) is archived, so recovery can find the
# first commit after T and stop there.
backrest check
echo "target time: $target"

step "stop the primary and restore to the target time"
compose stop primary
compose run --rm --no-deps -T -u postgres --entrypoint pgbackrest primary \
    --stanza=ebs --delta --type=time "--target=$target" --target-action=promote restore

step "start the restored primary"
compose up -d --no-deps primary
wait_for 180 "restored primary accepting connections" server_ready primary
promoted() { [[ "$(sql_super primary postgres "SELECT pg_is_in_recovery()")" == f ]]; }
wait_for 120 "recovery finished and promoted" promoted
expect_eq "$(sql_super primary ebs "SELECT string_agg(id::text, ',' ORDER BY id) FROM pitr")" \
    "1,2,3,4,5,6" "rows after point-in-time restore"

step "the restored cluster keeps archiving on its new timeline"
backrest check

step "PASS test_backup_restore.sh"
