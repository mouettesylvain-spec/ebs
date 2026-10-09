"""Static checks of the PostgreSQL deployment configs (P0-11 R2, R6); no Docker needed.

The deploy-smoke scripts in tests/deploy/ exercise the running stack (R1, R3, R4) and molecule
covers the Ansible role (R5); these tests pin the settings those runs rely on.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parents[3]
PG = REPO / "deploy" / "postgres"
CONF = PG / "conf"
ROLE = REPO / "deploy" / "ansible" / "roles" / "ebs_postgres"

_SETTING = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*=?\s*(.*?)\s*$")


def _strip_comment(line: str) -> str:
    # postgresql.conf comments start at an unquoted '#'.
    out, quoted = [], False
    for ch in line:
        if ch == "'":
            quoted = not quoted
        if ch == "#" and not quoted:
            break
        out.append(ch)
    return "".join(out).strip()


def _pg_settings() -> dict[str, str]:
    """postgresql.conf plus its include_dir fragments, in the order the server reads them."""
    settings: dict[str, str] = {}

    def read(path: Path) -> None:
        for raw in path.read_text().splitlines():
            line = _strip_comment(raw)
            if not line:
                continue
            match = _SETTING.match(line)
            assert match, f"{path.name}: cannot parse {raw!r}"
            key, value = match.group(1).lower(), match.group(2).strip("'")
            if key == "include_dir":
                assert value == "/etc/postgresql/conf.d", "compose mounts the fragments there"
                for fragment in sorted((CONF / "conf.d").glob("*.conf")):
                    read(fragment)
            else:
                settings[key] = value

    read(CONF / "postgresql.conf")
    return settings


def _hba_rules() -> list[list[str]]:
    rules = []
    for raw in (CONF / "pg_hba.conf").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            rules.append(line.split())
    return rules


def _ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(
        comment_prefixes=(";", "#"), inline_comment_prefixes=(";",), interpolation=None
    )
    parser.read_string(path.read_text())
    return parser


def _compose() -> dict[str, object]:
    data = YAML(typ="safe").load((PG / "compose.yaml").read_text())
    assert isinstance(data, dict)
    return data


def _role_defaults() -> dict[str, object]:
    data = YAML(typ="safe").load((ROLE / "defaults" / "main.yml").read_text())
    assert isinstance(data, dict)
    return data


# --- R2: authentication, TLS, secrets ----------------------------------------------------------


# R2
def test_hba_only_scram_or_peer_and_network_rules_require_tls() -> None:
    rules = _hba_rules()
    assert rules, "pg_hba.conf has no rules"
    for rule in rules:
        kind, method = rule[0], rule[-1]
        if kind == "hostnossl":
            assert method == "reject", rule
        elif kind == "local":
            # peer only for the OS postgres user (entrypoint, pgbackrest); everyone else: SCRAM.
            assert method == "scram-sha-256" or (method == "peer" and rule[2] == "postgres"), rule
        else:
            assert kind == "hostssl", f"network rules must require TLS: {rule}"
            assert method == "scram-sha-256", rule
            assert rule[3] not in {"0.0.0.0/0", "::/0", "all"}, f"open to the world: {rule}"
    assert ["hostnossl", "all", "all", "0.0.0.0/0", "reject"] in rules
    assert any(r[0] == "hostssl" and r[1] == "replication" for r in rules)


# R2
def test_server_tls_and_scram_password_encryption() -> None:
    settings = _pg_settings()
    assert settings["ssl"] == "on"
    assert settings["password_encryption"] == "scram-sha-256"
    assert settings["ssl_min_protocol_version"] == "TLSv1.2"
    assert settings["ssl_cert_file"].startswith("/var/lib/postgresql/tls/")
    assert settings["ssl_key_file"].startswith("/var/lib/postgresql/tls/")


# R2
def test_pgbouncer_uses_scram_and_tls_on_both_sides() -> None:
    ini = _ini(CONF / "pgbouncer.ini")["pgbouncer"]
    assert ini["auth_type"] == "scram-sha-256"
    assert ini["client_tls_sslmode"] == "require"
    assert ini["server_tls_sslmode"] == "require"
    # The userlist is generated from env at container start, never stored in the repo.
    assert ini["auth_file"].startswith("/run/")
    assert not (CONF / "userlist.txt").exists()


# R2
def test_secret_files_are_gitignored_and_example_is_committed() -> None:
    ignored = (REPO / ".gitignore").read_text().splitlines()
    assert "deploy/postgres/.env" in ignored
    assert "deploy/postgres/certs/" in ignored
    assert (PG / "env.example").is_file()
    assert not (PG / ".env").exists() or _is_ignored(PG / ".env")


def _is_ignored(path: Path) -> bool:
    git = shutil.which("git")
    if git is None:
        pytest.skip("missing dependency: git on PATH")
    done = subprocess.run([git, "check-ignore", "-q", str(path)], cwd=REPO, check=False)
    return done.returncode == 0


# R2
def test_env_example_lists_every_required_compose_variable_without_values() -> None:
    lines = (PG / "compose.yaml").read_text().splitlines()
    compose_text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    required = set(re.findall(r"\$\{([A-Z0-9_]+):\?", compose_text))
    assert {"POSTGRES_PASSWORD", "EBS_PASSWORD", "REPLICATION_PASSWORD"} <= required
    example = {}
    for raw in (PG / "env.example").read_text().splitlines():
        if raw.strip() and not raw.lstrip().startswith("#"):
            key, _, value = raw.partition("=")
            example[key.strip()] = value.strip()
    assert required <= example.keys(), f"missing from env.example: {required - example.keys()}"
    secrets = {k for k in example if k.endswith(("PASSWORD", "_PASS"))}
    assert secrets, "expected password keys in env.example"
    for key in secrets:
        assert example[key] == "", f"{key} must be empty in env.example"


# R2
def test_no_secrets_in_committed_configs() -> None:
    pattern = re.compile(r"(password|cipher-pass|secret)\s*=\s*\S", re.IGNORECASE)
    files = [*CONF.rglob("*"), PG / "compose.yaml", *ROLE.rglob("*")]
    for path in files:
        if not path.is_file() or path.suffix in {".md"}:
            continue
        for line in path.read_text().splitlines():
            if line.lstrip().startswith(("#", ";")):
                continue
            hit = pattern.search(line)
            # Templated or env-sourced values are fine; literals are not.
            if hit and not re.search(r"\{\{|\$\{|\$[A-Z_]+|%\(|:'", line):
                pytest.fail(f"{path.relative_to(REPO)}: literal secret? {line.strip()!r}")
    data = _compose()
    services = data["services"]
    assert isinstance(services, dict)
    for name, service in services.items():
        for key, value in (service.get("environment") or {}).items():
            if "PASSWORD" in key or "CIPHER_PASS" in key:
                assert str(value).startswith("${"), (name, key)
                assert ":?" in str(value), (name, key)


# R2
def test_gen_certs_script_makes_a_verifiable_self_signed_chain(tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("missing dependency: openssl on PATH")
    script = PG / "scripts" / "gen-certs.sh"
    assert os.access(script, os.X_OK)
    out = tmp_path / "certs"
    subprocess.run([str(script), str(out)], check=True, capture_output=True)
    for name in ("ca.crt", "server.crt", "server.key"):
        assert (out / name).is_file(), name
    assert stat.S_IMODE((out / "server.key").stat().st_mode) == 0o600
    assert not (out / "ca.key").exists() or stat.S_IMODE((out / "ca.key").stat().st_mode) == 0o600
    verify = subprocess.run(
        [openssl, "verify", "-CAfile", str(out / "ca.crt"), str(out / "server.crt")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verify.returncode == 0, verify.stderr
    sans = subprocess.run(
        [openssl, "x509", "-in", str(out / "server.crt"), "-noout", "-ext", "subjectAltName"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for host in ("primary", "standby", "pgbouncer", "localhost"):
        assert f"DNS:{host}" in sans
    # Re-running keeps existing certs (no surprise rotation under a running cluster).
    before = (out / "server.crt").read_bytes()
    subprocess.run([str(script), str(out)], check=True, capture_output=True)
    assert (out / "server.crt").read_bytes() == before


# --- R6: sizing for ~100 users + CI ------------------------------------------------------------


# R6
def test_postgres_connection_limits_and_timeouts() -> None:
    settings = _pg_settings()
    assert settings["max_connections"] == "100"
    assert settings["idle_in_transaction_session_timeout"] == "60s"


# R6
def test_pgbouncer_transaction_pooling_fits_under_max_connections() -> None:
    ini = _ini(CONF / "pgbouncer.ini")
    bouncer = ini["pgbouncer"]
    assert bouncer["pool_mode"] == "transaction"
    assert bouncer["default_pool_size"] == "20"
    max_db = int(bouncer["max_db_connections"])
    reserve = int(bouncer.get("reserve_pool_size", "0"))
    assert max_db + reserve < int(_pg_settings()["max_connections"])
    assert "ebs" in ini["databases"]


# R6
def test_ebs_role_statement_timeout_is_30s() -> None:
    init = (CONF / "initdb" / "10-ebs-roles.sh").read_text()
    assert re.search(r"ALTER ROLE ebs SET statement_timeout\s*=\s*'30s'", init)
    # Server-wide default stays unlimited so maintenance (pgbackrest, migrations) isn't cut off.
    assert "statement_timeout" not in _pg_settings()


# R5, R6
def test_ansible_role_defaults_match_compose_settings() -> None:
    defaults = _role_defaults()
    assert defaults["ebs_postgres_version"] == 17
    assert defaults["ebs_postgres_max_connections"] == 100
    assert defaults["ebs_postgres_idle_in_transaction_session_timeout"] == "60s"
    assert defaults["ebs_postgres_ebs_statement_timeout"] == "30s"
    assert defaults["ebs_pgbouncer_pool_mode"] == "transaction"
    assert defaults["ebs_pgbouncer_default_pool_size"] == 20
    # No password defaults: the play must supply them (from a vault).
    for key, value in defaults.items():
        if "password" in key or "cipher_pass" in key:
            assert value in (None, ""), key
    templates = {p.name: p.read_text() for p in (ROLE / "templates").iterdir()}
    assert "{{ ebs_postgres_max_connections }}" in templates["postgresql.conf.j2"]
    assert "scram-sha-256" in templates["pg_hba.conf.j2"]
    assert not re.search(r"\b(trust|md5|password)\s*$", templates["pg_hba.conf.j2"], re.M)
    assert "{{ ebs_pgbouncer_pool_mode }}" in templates["pgbouncer.ini.j2"]


# --- R1/R3 static wiring (the smoke scripts check behaviour) ------------------------------------


# R3
def test_wal_archiving_goes_to_pgbackrest() -> None:
    settings = _pg_settings()
    assert settings["wal_level"] == "replica"
    assert settings["archive_mode"] == "on"
    assert "pgbackrest" in settings["archive_command"]
    assert "archive-push" in settings["archive_command"]
    assert "archive-get" in settings["restore_command"]
    backrest = _ini(CONF / "pgbackrest.conf")
    assert backrest["ebs"]["pg1-path"] == "/var/lib/postgresql/data"
    assert backrest["global"]["repo1-cipher-type"] == "aes-256-cbc"
    assert "repo1-retention-full" in backrest["global"]


# R1
def test_compose_defines_primary_standby_and_pgbouncer() -> None:
    services = _compose()["services"]
    assert isinstance(services, dict)
    assert {"primary", "standby", "pgbouncer"} <= services.keys()
    assert services["standby"]["environment"]["EBS_PG_ROLE"] == "standby"
    assert services["primary"]["environment"]["EBS_PG_ROLE"] == "primary"
    for name in ("primary", "standby", "pgbouncer"):
        assert "healthcheck" in services[name], name
    # Only PgBouncer is published, and only on loopback.
    assert "ports" not in services["primary"]
    assert "ports" not in services["standby"]
    assert all(str(p).startswith("127.0.0.1:") for p in services["pgbouncer"]["ports"])
    volumes = _compose()["volumes"]
    assert isinstance(volumes, dict)
    assert "pgbackrest-repo" in volumes
