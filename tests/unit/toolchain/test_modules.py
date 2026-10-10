"""Module snapshots: Environment Modules / Lmod detection, env diff, install roots (P1-07)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.core.clock import FakeClock
from ebs.core.errors import ToolchainError
from ebs.meta.memory import InMemoryMetadataStore
from ebs.toolchain.fingerprint import fingerprint_roots
from ebs.toolchain.model import toolchain_id
from ebs.toolchain.modules import ModuleSnapshot, detect_module_system, snapshot_module
from ebs.toolchain.registry import register
from tests.helpers.modules import Flavour, ModuleSite, make_module_site, questa_like

FLAVOURS: list[Flavour] = ["envmodules", "lmod"]


@pytest.fixture(params=FLAVOURS)
def site(request: pytest.FixtureRequest, tmp_path: Path) -> ModuleSite:
    flavour: Flavour = request.param
    return make_module_site(tmp_path, flavour)


@pytest.fixture
def envmodules(tmp_path: Path) -> ModuleSite:
    return make_module_site(tmp_path, "envmodules")


def snap(
    site: ModuleSite,
    module: str = "questa/2025.2",
    *,
    user: str = "alice",
    home: str = "/home/alice",
    roots: tuple[Path, ...] = (),
) -> ModuleSnapshot:
    environ = site.environ(user=user, home=home)
    system = detect_module_system(environ)
    return snapshot_module(
        module, system=system, environ=environ, user=user, extra_roots=roots, timeout_s=30
    )


# R1
def test_register_captures_env_and_roots(envmodules: ModuleSite) -> None:
    site = envmodules
    top = questa_like(site)
    uvm = site.tool("uvm-1.2")
    s = snap(site, roots=(uvm,))

    assert s.module == "questa/2025.2"
    assert s.version == "2025.2"
    # The env diff: what purge + load changed, nothing from the clean starting shell.
    assert s.env["QUESTA_HOME"] == str(top)
    assert s.env["PATH"] == f"{top}/bin:/usr/bin:/bin"
    assert s.env["LD_LIBRARY_PATH"] == f"{top}/lib"
    assert s.env["QUESTA_MODE"] == "batch"
    assert s.env["LOADEDMODULES"] == "questa/2025.2"
    for unchanged in ("LANG", "HOME", "MODULEPATH", "MODULESHOME", "USER", "EBS_LEAK_CHECK"):
        assert unchanged not in s.env
    # Roots: the tree the module points into (bin/lib widened to it) plus the explicit --root.
    assert s.install_roots == tuple(sorted((top, uvm)))
    assert site.calls() == ["--version ", "purge ", "load questa/2025.2"]

    store = InMemoryMetadataStore(clock=FakeClock())
    reg = register(
        store,
        module=s.module,
        version=s.version,
        env=s.env,
        install_roots=s.install_roots,
        user="alice",
    )
    row = store.get_toolchain(reg.toolchain.id)
    assert row is not None
    assert row.fingerprint == fingerprint_roots([top, uvm])
    assert row.env == dict(s.env)
    assert row.install_roots == [str(r) for r in sorted((top, uvm))]
    assert row.registered_by == "alice"
    assert row.name == "questa"
    assert row.superseded_by is None
    assert row.id == toolchain_id("questa/2025.2", "2025.2", row.fingerprint, s.env)


# R1
def test_roots_skip_system_missing_and_module_internals(envmodules: ModuleSite) -> None:
    site = envmodules
    top = site.tool("vip/3.1", files={"etc/vip.cfg": "cfg"})
    nested = site.tool("vip/3.1/sub")  # nested inside top: collapses into it
    site.modulefile(
        "vip/3.1",
        "prepend-path PATH /usr/local/bin",
        "prepend-path PATH /bin",
        f"setenv VIP_CFG {top}/etc/vip.cfg",  # a file: its directory counts
        f"setenv VIP_SUB {nested}",
        f"setenv VIP_GONE {site.root}/does-not-exist",
        "setenv VIP_PORT 1234@licence.example.invalid",
        f"setenv VIP_MODULES {site.modulepath}/vip",
    )
    s = snap(site, "vip/3.1")
    assert s.install_roots == (top,)


# R1
def test_no_roots_needs_root_option(envmodules: ModuleSite) -> None:
    envmodules.modulefile("plain/1.0", "setenv PLAIN_MODE fast")
    with pytest.raises(ToolchainError, match="--root"):
        snap(envmodules, "plain/1.0")
    extra = envmodules.tool("plain-tree")
    assert snap(envmodules, "plain/1.0", roots=(extra,)).install_roots == (extra,)


# R1
def test_relative_root_rejected(envmodules: ModuleSite) -> None:
    questa_like(envmodules)
    with pytest.raises(ToolchainError, match="absolute"):
        snap(envmodules, roots=(Path("relative/dir"),))


# R1
@pytest.mark.parametrize("module", ["questa", "-x/1.0", "questa/", "a b/1", "questa/../x"])
def test_module_name_needs_name_and_version(envmodules: ModuleSite, module: str) -> None:
    with pytest.raises(ToolchainError, match="module"):
        snap(envmodules, module)
    assert [c for c in envmodules.calls() if c.startswith("load")] == []


# R5
def test_flavours(site: ModuleSite) -> None:
    environ = site.environ()
    system = detect_module_system(environ)
    assert system.flavour == site.flavour
    assert system.version == {"envmodules": "5.4.0", "lmod": "8.7.37"}[site.flavour]
    assert system.command == site.command

    top = questa_like(site)
    s = snap(site)
    assert s.system == system
    assert s.env["QUESTA_HOME"] == str(top)
    assert s.env["PATH"] == f"{top}/bin:/usr/bin:/bin"
    assert s.env["LOADEDMODULES"] == "questa/2025.2"
    assert s.install_roots == (top,)  # module-internal bookkeeping vars never become roots


# R5
def test_unknown_module_fails_in_both_flavours(site: ModuleSite) -> None:
    with pytest.raises(ToolchainError, match=r"nosuch/1\.0"):
        snap(site, "nosuch/1.0")


# R5
def test_modulefile_error_fails(site: ModuleSite) -> None:
    site.modulefile("broken/1.0", "fail license feature missing")
    with pytest.raises(ToolchainError, match="license feature missing"):
        snap(site, "broken/1.0")


# R5
def test_detect_prefers_lmod_cmd(tmp_path: Path) -> None:
    lmod = make_module_site(tmp_path / "a", "lmod")
    env_mod = make_module_site(tmp_path / "b", "envmodules")
    environ = {**env_mod.environ(), "LMOD_CMD": str(lmod.command)}
    system = detect_module_system(environ)
    assert (system.flavour, system.command) == ("lmod", lmod.command)


# R5
def test_detect_errors(tmp_path: Path, envmodules: ModuleSite) -> None:
    with pytest.raises(ToolchainError, match="MODULESHOME"):
        detect_module_system({"MODULEPATH": "/x"})
    empty = tmp_path / "empty-home"
    empty.mkdir()
    with pytest.raises(ToolchainError, match="neither"):
        detect_module_system({"MODULESHOME": str(empty)})
    envmodules.set_version("3.2.10")
    with pytest.raises(ToolchainError, match="4 or later"):
        detect_module_system(envmodules.environ())
    envmodules.set_version("garbage")
    (envmodules.command.parent / ".fake-flavour").write_text("lmod")  # prints the Lmod banner
    with pytest.raises(ToolchainError, match="version"):
        detect_module_system(envmodules.environ())


# R5
def test_modulepath_required(envmodules: ModuleSite) -> None:
    environ = envmodules.environ()
    system = detect_module_system(environ)
    del environ["MODULEPATH"]
    with pytest.raises(ToolchainError, match="MODULEPATH"):
        snapshot_module("questa/2025.2", system=system, environ=environ, user="alice")


# R6
def test_user_independent(envmodules: ModuleSite) -> None:
    site = envmodules
    top = questa_like(site)
    site.modulefile(
        "questa/2025.2",
        f"setenv QUESTA_HOME {top}",
        f"prepend-path PATH {top}/bin",
        "setenv QUESTA_CACHE $HOME/.questa",
    )
    a = snap(site, user="alice", home="/home/alice")
    b = snap(site, user="bob", home="/home/bob")
    assert a.env == b.env
    assert a.env["QUESTA_CACHE"] == "$HOME/.questa"
    for value in a.env.values():
        assert "alice" not in value
        assert "/home/" not in value

    store = InMemoryMetadataStore(clock=FakeClock())
    ids = {
        register(
            store,
            module=s.module,
            version=s.version,
            env=s.env,
            install_roots=s.install_roots,
            user=user,
        ).toolchain.id
        for s, user in ((a, "alice"), (b, "bob"))
    }
    assert len(ids) == 1


# R6
def test_registering_users_home_is_normalized(envmodules: ModuleSite) -> None:
    top = questa_like(envmodules)
    envmodules.modulefile("questa/2025.2", f"setenv QUESTA_HOME {top}", "setenv RC /home/al/rc")
    s = snap(envmodules, user="al", home="/home/al")
    assert s.env["RC"] == "$HOME/rc"


# R6
@pytest.mark.parametrize("value", ["/scratch/alice/x", "alice", "/a:/b/alice"])
def test_registering_user_name_rejected(envmodules: ModuleSite, value: str) -> None:
    top = questa_like(envmodules)
    envmodules.modulefile("questa/2025.2", f"setenv QUESTA_HOME {top}", f"setenv OWNER {value}")
    with pytest.raises(ToolchainError, match=r"OWNER.*alice"):
        snap(envmodules, user="alice")


# R6
@pytest.mark.parametrize("value", ["/opt/alice-tools", "/opt/malice/x", "alice.cfg"])
def test_user_name_inside_other_words_is_fine(envmodules: ModuleSite, value: str) -> None:
    top = questa_like(envmodules)
    envmodules.modulefile("questa/2025.2", f"setenv QUESTA_HOME {top}", f"setenv OTHER {value}")
    assert snap(envmodules, user="alice").env["OTHER"] == value
