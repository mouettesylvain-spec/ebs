"""`ebs release revalidate` (P3-05 R1, R3): the CLI over `ebs.release.revalidate`."""

from __future__ import annotations

import json
from pathlib import Path

from ebs.core.errors import ExitCode
from ebs.release.releases import create_release
from tests.helpers.signoff import signed_off


# R1, R3: by name and with --all; the re-run is a build of this site, reading the cache only.
def test_revalidate_command(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    store = s.services.memory_store
    archived = s.site.invoke(["release", "archive", "verif/cpu", "2026.10.1", "--to", "cold"])
    assert archived.exit_code == 0, archived.output

    shown = s.site.invoke(["release", "revalidate", "verif/cpu", "2026.10.1", "--domain", "test"])
    assert shown.exit_code == ExitCode.OK, shown.output
    assert "reproduced verif/cpu 2026.10.1" in shown.stdout
    assert "3 equivalent, 0 changed, 0 incomparable" in shown.stdout
    [record] = store.list_revalidations(s.release.id)
    assert record.build is not None
    assert store.get_build(record.build).cache_mode == "read"

    every = s.site.invoke(["release", "revalidate", "--all", "--domain", "test", "--json",
                           "--no-cache"])  # fmt: skip
    assert every.exit_code == ExitCode.OK, every.output
    [doc] = [json.loads(line) for line in every.stdout.splitlines()]
    assert (doc["release"], doc["verdict"]) == ("verif/cpu 2026.10.1", "reproduced")
    assert store.get_build(store.list_revalidations(s.release.id)[-1].build).cache_mode == "off"  # type: ignore[arg-type]

    # A test that fails on the re-run: differences, exit 1.
    s.services.scripts["sim[test=smoke]"] = ("fail",)
    differs = s.site.invoke(["release", "revalidate", "verif/cpu", "2026.10.1", "--domain",
                             "test", "--no-cache"])  # fmt: skip
    assert differs.exit_code == ExitCode.ACTIONS_FAILED, differs.output
    assert "differences verif/cpu 2026.10.1" in differs.stdout
    assert len(store.list_revalidations(s.release.id)) == 3

    changed = s.site.invoke(["release", "revalidate", "verif/cpu", "2026.10.1", "--domain",
                             "test", "--toolchain-override", "fake=fakesim/7.0"])  # fmt: skip
    assert changed.exit_code == ExitCode.USAGE
    assert "fakesim/7.0" in changed.output  # not registered: refused before anything runs
    assert len(store.list_revalidations(s.release.id)) == 3

    usage = s.site.invoke(["release", "revalidate", "--domain", "test"])
    assert usage.exit_code == ExitCode.USAGE
    assert "NAME VERSION" in usage.output
    bad = s.site.invoke(["release", "revalidate", "verif/cpu", "2026.10.1", "--domain", "test",
                         "--toolchain-override", "fake"])  # fmt: skip
    assert bad.exit_code == ExitCode.USAGE
    assert "NAME=latest" in bad.output


# R4: the quarterly `--all` run keeps going past a release it cannot revalidate, and a site-wide
# override for a toolchain a release does not use is skipped for that release.
def test_all_keeps_going(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    store = s.services.memory_store
    config = tmp_path / "config.toml"
    config.write_text(
        config.read_text() + '\n[revalidate]\ntoolchain_overrides = { questa = "latest" }\n'
    )
    second = create_release(
        store, name="verif/cpu", version="2026.10.2", build=s.release.build,
        outputs=["sim.result[*]"], by="alice",
    )  # fmt: skip
    for version, to in (("2026.10.1", "cold1"), ("2026.10.2", "cold2")):
        done = s.site.invoke(["release", "archive", "verif/cpu", version, "--to", to])
        assert done.exit_code == 0, done.output
    [broken] = store.list_archives(second.id)
    path = Path(broken.location)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b"\0")  # its digest no longer matches the record

    every = s.site.invoke(["release", "revalidate", "--all", "--domain", "test", "--json"])
    assert every.exit_code == ExitCode.ACTIONS_FAILED, every.output
    docs = {d["release"]: d for d in map(json.loads, every.stdout.splitlines())}
    assert docs["verif/cpu 2026.10.1"]["verdict"] == "reproduced"
    assert docs["verif/cpu 2026.10.2"]["verdict"] is None
    assert "does not match" in docs["verif/cpu 2026.10.2"]["error"]
    assert [r.verdict for r in store.list_revalidations(s.release.id)] == ["reproduced"]

    # Given on the command line, the same override is an error for a plan without it.
    strict = s.site.invoke(["release", "revalidate", "verif/cpu", "2026.10.1", "--domain",
                            "test", "--toolchain-override", "questa=latest"])  # fmt: skip
    assert strict.exit_code == ExitCode.USAGE
    assert "no toolchain 'questa'" in strict.output
