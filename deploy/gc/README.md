# EBS garbage collection (P1-09)

`ebs gc --domain D` collects one confidentiality domain: it deletes action-cache rows unused for
the domain's `ttl_days` (default 30), tombstones the CAS objects nothing keeps any more, and
deletes their bytes after `grace_hours` (default 24) if nobody used them meanwhile (invariant
I15). Above `high_water` × `quota_bytes` it evicts unrooted objects, least recently used first,
down to `low_water`. Releases, pinned builds (`ebs pin BUILD`) and the leases of running builds
are never collected. `ebs gc --dry-run` prints the same report without changing anything; every
run is recorded in the `gc_runs` table.

## Install (one timer per domain)

1. Create the `ebs-gc` account, a member of each domain's Unix group (and list those domains in
   `[gc].domains`: GC refuses any other), and a database role that
   can read and write the `ebs` schema.
2. `install -m 644 ebs-gc@.service ebs-gc@.timer /etc/systemd/system/`
3. `install -m 640 -g ebs-gc gc.toml.example /etc/ebs/gc.toml` and set the database URL and the
   CAS root.
4. `systemctl daemon-reload && systemctl enable --now ebs-gc@<domain>.timer` for every domain.

The unit allows writes only below `/cas/<domain>` (`ReadWritePaths`, with
`ProtectSystem=strict`); if `[cas].root` is elsewhere, change that line with
`systemctl edit ebs-gc@.service`. One collection per domain runs at a time: a second one (the
timer and a manual `ebs gc`) exits 2 and names the run that holds the domain's lock. A run that
crashed frees the lock after 6 hours; `ebs gc --dry-run` takes no lock.

Per-domain policy is set in the database, for example:

```sql
UPDATE ebs.domains SET ttl_days = 14, quota_bytes = 20 * 1024^4, high_water = 0.85,
       low_water = 0.75 WHERE name = 'cpu-nda';
```

GC is safe to run while builds run: builds touch what they use and lease what they need, and
every deletion is re-checked in the database at the moment it happens.
