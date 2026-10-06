from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ebs.cli._context import SiteServices, StatCacheSettings
from ebs.core.digest import hash_bytes
from ebs.core.errors import ExitCode
from ebs.sources.statcache import default_statcache_path
from tests.helpers.cli import Site, TestServices, poison_statcache, write_site


def _services(tmp_path: Path, config: str, *, default: Path | None = None) -> SiteServices:
    (tmp_path / "config.toml").write_text(config)
    return SiteServices(
        environ={"EBS_CONFIG": str(tmp_path / "config.toml"), "HOME": str(tmp_path / "home")},
        cwd=tmp_path,
        home=tmp_path / "home",
        statcache_default=default,
    )


def _roundtrip(services: SiteServices, warnings: list[str], probe: Path) -> bool:
    """Store an entry, reopen, and report whether it persisted."""
    probe.write_text("x")
    st = probe.stat()
    with services.open_statcache(warnings.append) as cache:
        # recorded well after the mtime, so the racy-clean rule does not turn the hit into a miss
        cache.store(st, probe, hash_bytes(b"x"), recorded_at=st.st_mtime + 60)
    with services.open_statcache(warnings.append) as cache:
        return cache.lookup(st, probe) is not None


# R7
def test_default_is_local(tmp_path: Path) -> None:
    assert default_statcache_path(1234) == Path("/var/tmp/ebs-1234/statcache.sqlite")
    services = _services(tmp_path, "")
    path = services.statcache_path()
    assert path == default_statcache_path(os.getuid())
    assert not path.is_relative_to(tmp_path / "home")  # never ~/.cache (often NFS)

    default = tmp_path / "var-tmp" / "ebs-me" / "statcache.sqlite"
    services = _services(tmp_path, "", default=default)
    warnings: list[str] = []
    assert _roundtrip(services, warnings, tmp_path / "probe")
    assert warnings == []
    assert stat.S_IMODE(default.parent.stat().st_mode) == 0o700


# R7
def test_config_override(tmp_path: Path) -> None:
    configured = tmp_path / "local" / "sc.sqlite"
    services = _services(
        tmp_path,
        f'[stat_cache]\npath = "{configured}"\n',
        default=tmp_path / "unused" / "statcache.sqlite",
    )
    assert services.statcache_path() == configured
    warnings: list[str] = []
    assert _roundtrip(services, warnings, tmp_path / "probe")
    assert configured.exists()
    assert not (tmp_path / "unused").exists()


# R7
def test_unwritable_degrades(tmp_path: Path) -> None:
    # A regular file where the directory should be: fails even as root (CI), unlike chmod.
    (tmp_path / "var-tmp").write_text("not a directory")
    services = _services(tmp_path, "", default=tmp_path / "var-tmp" / "ebs-me" / "s.sqlite")
    warnings: list[str] = []
    assert not _roundtrip(services, warnings, tmp_path / "probe")  # in memory: rehash next time
    assert len(warnings) == 2
    assert "var-tmp" in warnings[0]
    assert "rehash" in warnings[0]


# R7
def test_shared_default_dir_with_open_mode_degrades(tmp_path: Path) -> None:
    # /var/tmp is shared: a directory others can write to may hold a planted database.
    loose = tmp_path / "ebs-me"
    loose.mkdir(mode=0o755)
    loose.chmod(0o777)
    services = _services(tmp_path, "", default=loose / "statcache.sqlite")
    warnings: list[str] = []
    assert not _roundtrip(services, warnings, tmp_path / "probe")
    assert "0o777" in warnings[0] or "mode" in warnings[0]
    assert not (loose / "statcache.sqlite").exists()


# R7
def test_plan_still_works_when_degraded(tmp_path: Path) -> None:
    write_site(tmp_path, config_extra="")
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(line for line in config.read_text().splitlines() if "statcache" not in line)
    )
    (tmp_path / "var-tmp").write_text("not a directory")
    site = Site(tmp_path, TestServices(tmp_path))
    result = site.invoke(["plan"])
    assert result.exit_code == ExitCode.OK, result.output
    assert "stat cache" in result.stderr
    assert "warning" in result.stderr


def _gen_key(site: Site, *args: str) -> str:
    result = site.invoke(["plan", "--json", *args])
    assert result.exit_code == ExitCode.OK, result.output
    actions = json.loads(result.stdout)["actions"]
    return str(next(a["key"] for a in actions if a["action_id"] == "gen"))


# R7 (test-critic: settings must reach the snapshotter)
def test_stat_cache_settings_round_trip(tmp_path: Path) -> None:
    section = {"untrusted_mounts": [str(tmp_path)], "racy_window_s": 7, "audit_fraction": 0.5}
    settings = StatCacheSettings.from_section(section)
    assert settings == StatCacheSettings(None, 7.0, 0.5, (tmp_path,))


# R7
def test_untrusted_mount_ignores_poisoned_stat_cache(tmp_path: Path) -> None:
    write_site(tmp_path)
    site = Site(tmp_path, TestServices(tmp_path))
    honest = _gen_key(site)
    poison_statcache(tmp_path / "statcache.sqlite", site.proj / "src" / "a.txt", tmp_path / "cas")
    assert _gen_key(site) != honest  # control: the poisoned entry is used
    config = tmp_path / "config.toml"
    mount = f'[stat_cache]\nuntrusted_mounts = ["{site.proj / "src"}"]\n'
    config.write_text(config.read_text().replace("[stat_cache]\n", mount))
    assert _gen_key(Site(tmp_path, TestServices(tmp_path))) == honest


# R7
@pytest.mark.parametrize(
    "line",
    ["bogus = 1", "racy_window_s = true", "racy_window_s = -1", 'untrusted_mounts = "x"'],
)
def test_invalid_stat_cache_config_is_usage_error(tmp_path: Path, line: str) -> None:
    write_site(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(config.read_text() + line + "\n")  # appended to the [stat_cache] section
    result = Site(tmp_path, TestServices(tmp_path)).invoke(["plan"])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "[stat_cache]" in result.stderr


def _private_default(tmp_path: Path) -> Path:
    d = tmp_path / "ebs-me"
    d.mkdir(mode=0o700)
    return d / "statcache.sqlite"


# R7 (test-critic: a database planted by another user in /var/tmp)
def test_default_dir_owned_by_someone_else_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = _private_default(tmp_path)
    services = _services(tmp_path, "", default=default)
    monkeypatch.setattr("ebs.cli._context.os.getuid", lambda: os.geteuid() + 1)
    warnings: list[str] = []
    assert not _roundtrip(services, warnings, tmp_path / "probe")
    assert "belongs to uid" in warnings[0]
    assert not default.exists()


# R7
@pytest.mark.parametrize("mode", [0o770, 0o750, 0o701, 0o777])
def test_default_dir_open_to_others_degrades(tmp_path: Path, mode: int) -> None:
    default = _private_default(tmp_path)
    default.parent.chmod(mode)  # a mode-bit check, so this holds as root too
    warnings: list[str] = []
    assert not _roundtrip(_services(tmp_path, "", default=default), warnings, tmp_path / "probe")
    assert oct(mode) in warnings[0]
    assert not default.exists()


# R7 (test-critic: a symlink could point the WAL database at NFS)
def test_default_dir_symlink_degrades(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "ebs-me").symlink_to(real)
    default = tmp_path / "ebs-me" / "statcache.sqlite"
    warnings: list[str] = []
    assert not _roundtrip(_services(tmp_path, "", default=default), warnings, tmp_path / "probe")
    assert "not a directory" in warnings[0]
    assert not (real / "statcache.sqlite").exists()


# R7 (test-critic: a damaged database must not fail the plan)
def test_corrupt_statcache_degrades(tmp_path: Path) -> None:
    write_site(tmp_path)
    (tmp_path / "statcache.sqlite").write_bytes(b"not sqlite" * 100)
    result = Site(tmp_path, TestServices(tmp_path)).invoke(["plan"])
    assert result.exit_code == ExitCode.OK, result.output
    assert "unusable" in result.stderr
