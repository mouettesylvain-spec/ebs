"""Static checks of the metadata service deployment files (P1-06); no Docker needed."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

from ruamel.yaml import YAML

from ebs.config import load_config
from ebs.domains import Domains
from ebs.meta.pg import MetadataConfig
from ebs.meta.service.app import ServiceConfig
from ebs.meta.service.auth import Policy
from ebs.meta.service.authz import DomainPolicy, NssGroups

REPO = Path(__file__).resolve().parents[3]
META = REPO / "deploy" / "meta"
FACTORY = "ebs.meta.service.app:app_from_config"


def test_factory_target_exists() -> None:
    module, _, name = FACTORY.partition(":")
    assert callable(getattr(importlib.import_module(module), name))


def test_compose_service() -> None:
    compose = YAML(typ="safe").load((META / "compose.yaml").read_text())
    assert compose["include"][0]["path"] == "../postgres/compose.yaml"
    meta = compose["services"]["meta"]
    assert meta["build"]["dockerfile"] == "deploy/meta/Containerfile"
    assert any("/readyz" in part for part in meta["healthcheck"]["test"])  # R6
    assert meta["depends_on"]["pgbouncer"]["condition"] == "service_healthy"
    assert all(p.startswith("127.0.0.1:") for p in meta["ports"])  # TLS proxy in front
    assert "./config.toml:/etc/ebs/config.toml:ro" in meta["volumes"]
    assert FACTORY in (META / "Containerfile").read_text()


def test_systemd_unit() -> None:
    unit = (META / "systemd" / "ebs-meta.service").read_text()
    assert FACTORY in unit
    assert "User=ebs-meta" in unit
    assert "NoNewPrivileges=yes" in unit


def test_example_config_is_valid() -> None:
    config = tomllib.loads((META / "config.toml.example").read_text())
    MetadataConfig.from_mapping(config["metadata"])
    service = ServiceConfig.from_mapping(config["meta_service"])
    # P2-01: the served domains are configured, so the deployed policy builds.
    policy = DomainPolicy(
        Policy(), Domains.from_config(load_config([META / "config.toml.example"])),
        service.domains, NssGroups(), service.token_domains,
    )  # fmt: skip
    assert policy.served == {"cpu-nda"}
    assert "deploy/meta/config.toml" in (REPO / ".gitignore").read_text().splitlines()
