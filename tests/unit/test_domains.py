"""`[[domains]]` config (P2-01, D7): parsing, merging and per-domain roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.config import load_config
from ebs.core.errors import ConfigError
from ebs.domains import Domains, current_groups
from ebs.runner.config import RunnerSettings


def test_parse_and_merge(tmp_path: Path) -> None:
    site = tmp_path / "site.toml"
    site.write_text(
        '[cas]\nroot = "/cas"\n[debug]\nroot = "/debug"\n'
        '[[domains]]\nname = "cpu-nda"\nunix_group = "cpu"\nquota = "50T"\n'
        '[[domains]]\nname = "pdk"\nunix_group = "pdk-ro"\ncas_root = "/pdk-cas"\n'
        'debug_root = "/pdk-debug"\nttl_days = 365\n'
    )
    user = tmp_path / "user.toml"
    user.write_text('[[domains]]\nname = "cpu-nda"\nunix_group = "cpu2"\n')
    config = load_config([user, site])  # highest precedence first
    domains = Domains.from_config(config)
    assert sorted(domains.by_name) == ["cpu-nda", "pdk"]
    assert domains.require("cpu-nda").unix_group == "cpu2"  # the user file wins, per domain
    assert domains.require("cpu-nda").quota_bytes is None  # whole entry replaced
    pdk = domains.require("pdk")
    assert (pdk.ttl_days, pdk.cas_root) == (365, Path("/pdk-cas"))
    settings = RunnerSettings.from_config(config, {})
    assert settings.cas_root_for("pdk") == Path("/pdk-cas")
    assert settings.cas_root_for("cpu-nda") == Path("/cas")
    assert settings.debug_root_for("pdk") == Path("/pdk-debug")
    assert settings.debug_root_for("other") == Path("/debug")
    assert domains.is_member("pdk", {"pdk-ro"})
    assert not domains.is_member("pdk", {"cpu2"})
    assert not domains.is_member("unknown", {"cpu2"})
    assert Domains.from_config({}).is_member("anything", set())  # unconfigured: unrestricted
    with pytest.raises(ConfigError, match="not configured"):
        domains.require("unknown")


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('unix_group = "g"\n', "needs a name"),
        ('name = "a"\n', "unix_group is required"),
        ('name = "a b"\nunix_group = "g"\n', "invalid domain name"),
        ('name = "a"\nunix_group = "g"\ncas_root = "rel"\n', "absolute path"),
        ('name = "a"\nunix_group = "g"\nquota = "lots"\n', "size like"),
        ('name = "a"\nunix_group = "g"\nttl_days = 0\n', "positive integer"),
        ('name = "a"\nunix_group = "g"\ncolour = 1\n', "unknown keys"),
    ],
)
def test_invalid(tmp_path: Path, table: str, message: str) -> None:
    path = tmp_path / "c.toml"
    path.write_text("[[domains]]\n" + table)
    with pytest.raises(ConfigError, match=message):
        Domains.from_config(load_config([path]))


def test_duplicate_name(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text('[[domains]]\nname = "a"\nunix_group = "g"\n' * 2)
    with pytest.raises(ConfigError, match="listed twice"):
        load_config([path])


def test_current_groups() -> None:
    names = {1: "staff", 2: "nda"}

    def name_of(gid: int) -> str:
        return names[gid]

    assert current_groups(getgroups=lambda: [2, 3], getgid=lambda: 1, name_of=name_of) == {
        "staff", "nda",
    }  # fmt: skip
