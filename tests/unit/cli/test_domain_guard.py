"""The CLI refuses a flow of a domain the user is not in, before any hashing (P2-01 R3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.core.errors import ExitCode
from tests.helpers.cli import Site, TestServices, write_site

DOMAINS = '[[domains]]\nname = "test"\nunix_group = "nda-test"\n'


class GroupServices(TestServices):
    __test__ = False

    def __init__(self, root: Path, groups: set[str]) -> None:
        super().__init__(root)
        self.groups = frozenset(groups)

    def user_groups(self) -> frozenset[str]:
        return self.groups


def _site(tmp_path: Path, groups: set[str], config: str = DOMAINS) -> Site:
    write_site(tmp_path, config_extra=config)
    return Site(tmp_path, GroupServices(tmp_path, groups))


# R3: outside the domain's group, plan and build stop before the stat cache or CAS is touched.
@pytest.mark.parametrize("command", [["plan"], ["build"]])
def test_refused_before_hashing(tmp_path: Path, command: list[str]) -> None:
    site = _site(tmp_path, {"users", "other-nda"})
    result = site.invoke(command)
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "not a member of domain 'test'" in result.output
    assert "nda-test" in result.output
    assert not (tmp_path / "cas").exists()  # nothing snapshotted
    assert not (tmp_path / "statcache.sqlite").exists()  # nothing hashed
    assert not (tmp_path / "proj" / ".ebs").exists()  # no build recorded
    assert site.services.memory_store.list_releases("test") == []


# R3: members plan as before; a site without [[domains]] is not restricted.
def test_members_and_unconfigured_sites(tmp_path: Path) -> None:
    member = _site(tmp_path / "member", {"nda-test"})
    assert member.invoke(["plan"]).exit_code == ExitCode.OK
    open_site = _site(tmp_path / "open", set(), config="")
    assert open_site.invoke(["plan"]).exit_code == ExitCode.OK


# R3: with [[domains]] configured, a flow naming an unconfigured domain is refused.
def test_unconfigured_domain_refused(tmp_path: Path) -> None:
    other = '[[domains]]\nname = "cpu-nda"\nunix_group = "nda-test"\n'
    site = _site(tmp_path, {"nda-test"}, config=other)
    result = site.invoke(["plan"])
    assert result.exit_code == ExitCode.USAGE
    assert "domain 'test' is not configured" in result.output
