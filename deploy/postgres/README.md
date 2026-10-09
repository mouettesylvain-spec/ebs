# EBS PostgreSQL deployment

The metadata database for EBS is PostgreSQL 17. This directory deploys it as a primary with a
streaming hot standby, PgBouncer for connection pooling, and pgBackRest for backups with
point-in-time recovery (PITR). Two forms are provided:

- `compose.yaml` runs everything on one host with Docker Compose or Podman Compose. Use it for a
  lab, for CI, or for a small site.
- `deploy/ansible/roles/ebs_postgres` sets up the same thing on Debian/Ubuntu hosts: one primary
  host and one standby host, each running PgBouncer.

```
clients (ebs CLI, runners, CI) ──TLS──▶ PgBouncer :6432 ──TLS──▶ primary :5432 ──stream──▶ standby
                                       transaction pool              │ archive_command
                                       20 conns per db/user          ▼
                                                                pgBackRest repo (encrypted)
```

| Setting | Value | Why |
| --- | --- | --- |
| `max_connections` | 100 | Clients connect to PgBouncer, not to the server |
| PgBouncer `pool_mode` / `default_pool_size` | `transaction` / 20 | About 100 users and CI share a few server connections |
| PgBouncer `max_db_connections` + `reserve_pool_size` | 80 + 5 | Leaves room under 100 for replication and admins |
| `idle_in_transaction_session_timeout` | 60 s | A forgotten open transaction would hold locks and block vacuum |
| `statement_timeout` for role `ebs` | 30 s | Set on the role, so pgBackRest and admin sessions have no limit |
| Authentication | `scram-sha-256` and `hostssl` only | Plain-text TCP is rejected in `pg_hba.conf` |

## Install (compose)

```sh
cd deploy/postgres
cp env.example .env && chmod 600 .env     # then set every password in .env
scripts/gen-certs.sh                      # self-signed CA + server cert in ./certs (git-ignored)
docker compose up -d --build              # or: podman compose up -d --build
docker compose ps                         # all three services become "healthy"
```

EBS then connects through PgBouncer:
`postgresql+psycopg://ebs:<EBS_PASSWORD>@<host>:6432/ebs?sslmode=require`. PgBouncer is published
only on `127.0.0.1` of the Docker host. Expose it more widely with a firewall rule or a reverse
proxy, never by publishing the server ports. To use a site CA instead of the self-signed one,
put its `ca.crt`, `server.crt` and `server.key` into `certs/`, or point `EBS_PG_CERT_DIR` at them.

Secrets live only in `.env`, which is git-ignored, and every password variable is required.
**Keep `PGBACKREST_REPO1_CIPHER_PASS` in your secret store as well**: without it, the backups
cannot be read.

Check replication at any time:

```sh
docker compose exec -u postgres primary \
  psql -Atc "SELECT application_name, state, replay_lag FROM pg_stat_replication"
# standby1|streaming|…
```

## Backups

On first start, the primary creates the pgBackRest stanza `ebs`. `archive_command` then sends
every WAL segment to the repository (the `pgbackrest-repo` volume, encrypted with AES-256).
Schedule backups from cron or a systemd timer on the Docker host:

```sh
# weekly full, daily differential
docker compose exec -T -u postgres primary pgbackrest --stanza=ebs --type=full backup
docker compose exec -T -u postgres primary pgbackrest --stanza=ebs --type=diff backup
docker compose exec -T -u postgres primary pgbackrest --stanza=ebs info
```

Retention keeps 2 full backups and 6 differential backups (`conf/pgbackrest.conf`). In
production, put the repository on another machine (pgBackRest `repo1-host`) or in object
storage (`repo1-type=s3`). If it stays a volume on the database host, it is lost together with
that host.

## Failover runbook (manual promotion)

Use this when the primary is lost or has to be taken down for longer than clients can wait.
`tests/deploy/test_compose.sh` runs these exact steps.

1. **Check that the primary is really down, and fence it.** Two primaries means split brain.
   ```sh
   docker compose stop primary
   ```
2. **Promote the standby.**
   ```sh
   docker compose exec -u postgres standby psql -c "SELECT pg_promote(wait => true)"
   docker compose exec -u postgres standby psql -Atc "SELECT pg_is_in_recovery()"   # f
   ```
3. **Point PgBouncer at the new primary and reload it.** Client connection strings do not change.
   ```sh
   scripts/pgbouncer-switch.sh standby
   echo EBS_PG_PRIMARY_HOST=standby >> .env       # so a PgBouncer restart keeps the switch
   ```
   PgBouncer reconnects its pools on the next transaction. Clients only see the transactions
   that were in flight fail.

   > **Split brain warning:** after a failover, a plain `docker compose up -d` would start the
   > old `primary` again as a second primary, and it would archive timeline 1 into the same
   > stanza. Before any later `up`, remove it: `docker compose rm -sf primary` and delete its
   > `pg-primary` volume.
4. **Back up the new timeline.** It has archived from the moment it was promoted, but take a
   full backup now:
   `docker compose exec -u postgres standby pgbackrest --stanza=ebs --type=full backup`.
5. **Rebuild redundancy.** The old primary must not start again as a primary. Wipe its volume
   and re-clone it as a standby of the new primary. Alternatively, run `pg_rewind`
   (`wal_log_hints` is on) and then `pg_basebackup` only if the rewind fails. In this compose
   file the services have fixed roles, so the simplest way back is to switch roles back with a
   planned failover in a maintenance window: take a fresh backup, then recreate both volumes from
   that backup.

Automatic failover (Patroni with etcd/Consul, or `pg_auto_failover`) is a possible later step.
It is not built here, because a manual, deliberate promotion is safer than an automated one
until the site has a reliable fencing mechanism.

## Restore runbook (point-in-time recovery)

`tests/deploy/test_backup_restore.sh` runs these steps against a scratch stack.

1. Find the target time: the last good moment, in the server's time zone (UTC in the image).
   Check that a backup older than that time exists:
   `docker compose exec -u postgres primary pgbackrest --stanza=ebs info`.
2. Stop the clients (PgBouncer) and the server:
   ```sh
   docker compose stop pgbouncer primary
   ```
3. Restore in place. `--delta` reuses the files that are still correct:
   ```sh
   docker compose run --rm --no-deps -u postgres --entrypoint pgbackrest primary \
     --stanza=ebs --delta --type=time "--target=2026-10-09 14:30:00+00" \
     --target-action=promote restore
   ```
   To restore the latest state instead (after losing the data volume), leave out `--type` and
   `--target`.
4. Start the server, check the data, then start PgBouncer:
   ```sh
   docker compose up -d --no-deps primary
   docker compose exec -u postgres primary psql -Atc "SELECT pg_is_in_recovery()"   # f
   docker compose up -d pgbouncer
   ```
5. The restored primary starts a new timeline, so the standby has to be re-cloned. Remove its
   volume and start it again: `docker compose rm -sf standby && docker volume rm <project>_pg-standby && docker compose up -d standby`.
   pgBackRest does not restore replication slots. When the standby clones, its entrypoint
   recreates the `standby1` slot if it is missing (`pg_basebackup --create-slot`). Then take a
   full backup.

## Ansible role

```yaml
- hosts: ebs_db_primary
  become: true
  roles: [{ role: ebs_postgres }]
- hosts: ebs_db_standby
  become: true
  roles:
    - role: ebs_postgres
      vars: { ebs_postgres_replication_role: standby, ebs_postgres_primary_host: db1.example.org }
```

The four secrets (`ebs_postgres_superuser_password`, `ebs_postgres_ebs_password`,
`ebs_postgres_replication_password`, `ebs_pgbackrest_cipher_pass`) have no defaults and must
come from Ansible Vault. The role is idempotent: a second run reports `changed=0`.

Two things differ from the compose stack:

- **PgBouncer on every host points at its local server.** On the standby host, that server is
  read-only, so clients must use the primary's PgBouncer through a DNS name or a VIP that you
  move during a failover.
- **Each host has its own local pgBackRest repository** (`ebs_pgbackrest_repo_path`), and the
  role creates the stanza only on the primary. **Right after promoting a standby host, run
  `sudo -u postgres pgbackrest --stanza=ebs stanza-create` and then a full backup on it.**
  Otherwise its `archive_command` fails and WAL piles up until the disk is full. A shared or
  remote repository (`repo1-host`, S3) is a follow-up.

To test the role:

```sh
cd deploy/ansible/roles/ebs_postgres
uvx --from "molecule>=24" --with "molecule-plugins[docker]" --with ansible-core --with docker molecule test
```

Molecule converges a primary in a Debian 12 container that runs systemd, checks idempotence,
then runs `verify.yml`: a query through PgBouncer over TLS, the R6 settings, and `pgbackrest check`.
The standby path (`tasks/standby.yml`) is not covered by molecule. `test_compose.sh` covers the
same steps for the compose stack.

## Tests

| Test | Needs | What it checks |
| --- | --- | --- |
| `tests/unit/deploy/test_configs.py` | nothing (runs in `make check`) | auth/TLS/secret hygiene (R2), sizing (R6), wiring |
| `tests/deploy/test_compose.sh` | Docker or `COMPOSE="podman compose"` | R1 pooling + replication, R2 TLS, R4 failover |
| `tests/deploy/test_backup_restore.sh` | Docker or Podman | R3 full backup, WAL archive, PITR |
| `molecule test` (role dir) | Docker | R5 idempotence |

The last three run in the manual GitHub Actions workflow `deploy-smoke`
(`.github/workflows/deploy-smoke.yml`).
