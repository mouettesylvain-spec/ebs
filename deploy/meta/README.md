# EBS metadata service (P1-06)

Runners on compute nodes and CLIs on login nodes reach the metadata database only through this
service, so no database credential ever lands on a cluster node. It is a FastAPI app
(`ebs.meta.service.app:app_from_config`) over `PgMetadataStore`; the REST API lives under
`/api/v1` (OpenAPI at `/api/v1/openapi.json`).

## Deploy

- **Containers:** `compose.yaml` adds the `meta` service to the PostgreSQL stack of
  `../postgres` (it `include`s that file). Copy `config.toml.example` to `config.toml`, set the
  `ebs` role's password, `chmod 600` it, then `docker compose up -d --build`.
- **systemd:** `systemd/ebs-meta.service` runs uvicorn from a virtualenv in `/opt/ebs`.

Put the site's TLS reverse proxy in front of `127.0.0.1:8080`. Run the database migrations
before starting a new version: `/readyz` answers 503 until the schema is at head.

## Probes

- `GET /healthz`: the process serves requests.
- `GET /readyz`: the database is reachable and its schema is at the migrations' head.

## Authentication (decision D1)

- **MUNGE** (cluster hosts): clients send `X-EBS-Munge` with a credential whose payload binds it
  to this service and request: `munge --string '["ebs-meta/1","<audience>","<METHOD>","<path>"]'`
  (`path` below `/api/v1`, percent-decoded). The service runs `unmunge`, which refuses replayed
  and expired credentials, checks the payload against `[meta_service].audience` and the request,
  and maps the uid to a user name via NSS. A credential captured from other MUNGE traffic (SLURM)
  is therefore useless here. munged must run on the service host with the cluster's key
  (`/run/munge` is mounted into the container).
- **Bearer tokens** (CI, services): `python -m ebs.meta.service token create ci-cpu --kind ci
  --team rv32x-cpu` prints a token once; only its SHA-256 is stored. Clients set
  `[metadata].token_file` (mode 600) or `$EBS_META_TOKEN`. `token revoke NAME` revokes them.

Check credentials with
`curl -H "X-EBS-Munge: $(munge -s '["ebs-meta/1","ebs-meta.example","GET","/whoami"]')" https://ebs-meta.example/api/v1/whoami`.

Ownership (R4): a build is changed only by its owner (`user_name`) or by a CI token whose team
is the build's project. Service tokens can read and write the action cache but not builds.
Registering a toolchain makes it current for every later build, so only service tokens and the
users in `[meta_service].toolchain_admins` may.

Domains (P2-01): the service serves only `[meta_service].domains`, each configured in
`[[domains]]`; run it as an account that is a member of exactly those domains' groups. A user
sees a domain only if NSS (LDAP/SSSD) puts them in its `unix_group` (cached `group_ttl_s`, 60 s
by default, so a removal takes effect within a minute). Bearer tokens are scoped with
`token_domains = { "ci-cpu" = ["cpu-nda"] }`. Outside their domains callers get 403, or 404 for
a build or release named by id, and every cache lookup is a miss. Create a domain's directories
with `deploy/admin/create_domain.sh NAME GROUP` and verify them with `ebs domain check`.

## Client configuration

```toml
[metadata]
url = "https://ebs-meta.example"   # the service root; routes are under /api/v1
# auth = "munge"                   # default without a token; "token" with token_file/$EBS_META_TOKEN
# audience = "ebs-meta.example"   # MUNGE binding; default: host[:port] of url
# token_file = "~/.config/ebs/meta-token"
# ca_file = "/etc/pki/tls/certs/site-ca.pem"
# timeout_s = 10
# retries = 4
```
