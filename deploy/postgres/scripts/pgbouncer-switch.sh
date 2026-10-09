#!/usr/bin/env bash
# Point PgBouncer at another PostgreSQL host and reload it (failover step 3, see README.md).
#
#   scripts/pgbouncer-switch.sh HOST
#
# EBS_COMPOSE overrides the compose command, e.g. "podman compose" or
# "docker compose -p myproj --env-file /path/.env -f /path/compose.yaml".
# The switch lasts until PgBouncer restarts: also set EBS_PG_PRIMARY_HOST=HOST in .env.
set -euo pipefail

host=${1:?usage: pgbouncer-switch.sh HOST}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
read -r -a compose <<<"${EBS_COMPOSE:-docker compose -f $here/compose.yaml}"

"${compose[@]}" exec -T pgbouncer ebs-entrypoint.sh switch-primary "$host"
