"""`ebs domain list|check` (P2-01 R4) on directories laid out like create_domain.sh makes them."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ebs.core.errors import ExitCode
from tests.helpers.cli import Site, TestServices, write_site

GID = os.getgid()


class DomainServices(TestServices):
    __test__ = False

    def __init__(self, root: Path, *, free: tuple[int, int] = (500, 1000)) -> None:
        super().__init__(root)
        self.free = free

    def group_id(self, name: str) -> int | None:
        return {"nda-test": GID, "nda-other": GID + 1}.get(name)

    def disk_usage(self, path: Path) -> tuple[int, int]:
        return self.free

    def user_groups(self) -> frozenset[str]:
        return frozenset({"nda-test"})


def _layout(root: Path, name: str) -> None:
    base = root / "cas" / name
    base.mkdir(parents=True)
    base.chmod(0o2750)
    for sub in ("blobs", "trees", "tmp"):
        (base / sub).mkdir()
        (base / sub).chmod(0o2770)
    (root / "debug" / name).mkdir(parents=True)
    (root / "debug" / name).chmod(0o2770)


def _site(tmp_path: Path, **kwargs: object) -> Site:
    config = (
        f'[debug]\nroot = "{tmp_path / "debug"}"\n'
        '[[domains]]\nname = "test"\nunix_group = "nda-test"\nquota = "2T"\n'
        '[[domains]]\nname = "partner"\nunix_group = "nda-other"\nttl_days = 90\n'
    )
    write_site(tmp_path, config_extra=config)
    return Site(tmp_path, DomainServices(tmp_path, **kwargs))  # type: ignore[arg-type]


# R4: list shows each domain with its group, roots, TTL, quota and membership.
def test_list(tmp_path: Path) -> None:
    site = _site(tmp_path)
    result = site.invoke(["domain", "list", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    docs = {d["name"]: d for d in json.loads(result.stdout)["domains"]}
    assert docs["test"]["member"] is True
    assert docs["partner"]["member"] is False
    assert docs["test"]["quota_bytes"] == 2 << 40
    assert docs["partner"]["ttl_days"] == 90
    assert docs["test"]["cas"] == str(tmp_path / "cas" / "test")
    assert "not a member" in site.invoke(["domain", "list"]).stdout


# R4: check passes on a correct layout and names every drift: mode, group, missing dir, space.
def test_check(tmp_path: Path) -> None:
    _layout(tmp_path, "test")
    _layout(tmp_path, "partner")
    site = _site(tmp_path)
    result = site.invoke(["domain", "check", "--json"])
    doc = json.loads(result.stdout)
    by_name = {d["domain"]: d for d in doc["domains"]}
    assert by_name["test"]["ok"] is True, by_name["test"]
    assert by_name["partner"]["ok"] is False  # its group is another gid
    partner = [p for d in by_name["partner"]["dirs"] for p in d["problems"]]
    assert len(partner) == 5  # every directory: the wrong group
    assert all("expected 'nda-other'" in p for p in partner)
    assert result.exit_code == ExitCode.INFRA

    (tmp_path / "cas" / "test" / "tmp").chmod(0o700)
    (tmp_path / "debug" / "test").rmdir()
    text = site.invoke(["domain", "check"])
    assert text.exit_code == ExitCode.INFRA
    assert f"{tmp_path / 'cas' / 'test' / 'tmp'}: mode 0700, expected 2770" in text.stdout
    assert f"{tmp_path / 'debug' / 'test'}: missing" in text.stdout

    full = _site(tmp_path / "full", free=(50, 1000))
    _layout(tmp_path / "full", "test")
    _layout(tmp_path / "full", "partner")
    low = full.invoke(["domain", "check", "--min-free", "10"])
    assert "only 5% free" in low.stdout


def test_check_needs_domains(tmp_path: Path) -> None:
    write_site(tmp_path)
    site = Site(tmp_path, TestServices(tmp_path))
    result = site.invoke(["domain", "check"])
    assert result.exit_code == ExitCode.USAGE
    assert "no domains configured" in result.output
    assert "unrestricted" in site.invoke(["domain", "list"]).stdout
