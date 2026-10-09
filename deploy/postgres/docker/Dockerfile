# One image for the primary, the standby and PgBouncer: the official PostgreSQL 17 image plus
# pgBackRest and PgBouncer from the PGDG apt repository it already configures.
FROM docker.io/library/postgres:17-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends pgbackrest pgbouncer \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o postgres -g postgres -m 0750 \
        /var/lib/pgbackrest /var/log/pgbackrest /etc/pgbackrest /run/pgbouncer

COPY ebs-entrypoint.sh /usr/local/bin/ebs-entrypoint.sh

ENTRYPOINT ["ebs-entrypoint.sh"]
CMD ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]
