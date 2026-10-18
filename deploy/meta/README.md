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

## Dashboard login, limits, events and metrics (P2-05)

- **Login:** with `[meta_service.oidc]` set (see `config.toml.example`), `/auth/login` signs
  users in with GitLab (authorization code + PKCE). The GitLab user name (`username_claim`) must
  be the Unix user name: authorization uses its NSS groups like MUNGE. Sessions are signed
  cookies valid `session_ttl_s` (8 h); to end every session at once, put a new key on the first
  line of `session_secret_file` and remove the old ones. Serve the service over HTTPS
  (`cookie_secure = true`); the issuer must be https too. GitLab user names must be the Unix
  names and must not be choosable: take them from LDAP, or forbid username changes in GitLab,
  or a user could take the name of a Unix account that never logged in to GitLab. Rate-limit
  `/auth/*` at the proxy (per client address): each callback makes a request to GitLab.
- **Limits:** `[meta_service.limits]` (per principal, per worker process); clients retry 429s.
- **Events:** `GET /api/v1/builds/{id}/events` and `/api/v1/domains/{d}/events` are Server-Sent
  Events. Put no buffering proxy in front of them (nginx: `proxy_buffering off`; the service
  also sends `X-Accel-Buffering: no`) and allow `max_stream_s` (300 s) read timeouts. The
  `events` table is partitioned by month; install the daily retention job:
  `install -m 644 systemd/ebs-meta-prune.* /etc/systemd/system/ && systemctl enable --now
  ebs-meta-prune.timer` (`events prune --dry-run` lists what it would drop).
- **Metrics:** `GET /metrics` is Prometheus text, without credentials: let only the scraper reach
  it. Counters are per worker process: scrape each worker, or run one worker per port.
- **Logs:** JSON lines on stderr (journald), each with `request_id` (also the `X-Request-ID`
  response header; a caller's well-formed `X-Request-ID` is kept).

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
