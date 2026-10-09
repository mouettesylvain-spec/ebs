#!/usr/bin/env bash
# First-start initialisation of the primary (run by the postgres image entrypoint as OS user
# postgres, against the temporary socket-only server). Passwords come from the environment and
# are passed as psql variables, never interpolated into SQL text.
set -euo pipefail

: "${EBS_PASSWORD:?EBS_PASSWORD must be set (see deploy/postgres/env.example)}"
: "${REPLICATION_PASSWORD:?REPLICATION_PASSWORD must be set (see deploy/postgres/env.example)}"

psql -v ON_ERROR_STOP=1 --no-psqlrc --username postgres --dbname postgres \
    -v ebs_password="$EBS_PASSWORD" \
    -v replication_password="$REPLICATION_PASSWORD" <<'SQL'
CREATE ROLE ebs LOGIN PASSWORD :'ebs_password';
-- Application sessions: no runaway queries; PgBouncer logs in as this role, so it applies there.
ALTER ROLE ebs SET statement_timeout = '30s';
ALTER ROLE ebs SET idle_in_transaction_session_timeout = '60s';

CREATE ROLE replicator LOGIN REPLICATION PASSWORD :'replication_password';

CREATE DATABASE ebs OWNER ebs;
REVOKE ALL ON DATABASE ebs FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE ebs TO ebs;

SELECT pg_create_physical_replication_slot('standby1');
SQL

# The backup stanza needs the cluster running (it is, on the init socket) and the repo volume.
pgbackrest --stanza=ebs stanza-create
