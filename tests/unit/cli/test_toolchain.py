"""`ebs toolchain register|list|show|check` (P1-07)."""

from __future__ import annotations

import json
import shutil

from ebs.core.errors import ExitCode
from ebs.toolchain.registry import RegistryToolchainResolver
from tests.helpers.cli import Site
from tests.helpers.modules import ModuleSite, make_module_site, questa_like


def _modules(site: Site) -> ModuleSite:
    mods = make_module_site(site.root / "mods", "lmod")
    site.services.environ.update(mods.environ(user="alice", home=str(site.root / "home")))
    return mods


# R1
def test_register_list_show(site: Site) -> None:
    mods = _modules(site)
    top = questa_like(mods)
    uvm = mods.tool("uvm")
    result = site.invoke(["toolchain", "register", "questa/2025.2", "--root", str(uvm), "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    doc = json.loads(result.stdout)
    assert doc["status"] == "registered"
    assert doc["install_roots"] == sorted([str(top), str(uvm)])
    assert doc["module_system"] == {"flavour": "lmod", "version": "8.7.37"}

    listed = json.loads(site.invoke(["toolchain", "list", "--json"]).stdout)["toolchains"]
    assert [t["id"] for t in listed] == [doc["id"]]
    assert listed[0]["registered_by"] == "alice"

    shown = site.invoke(["toolchain", "show", "questa/2025.2"])
    assert shown.exit_code == ExitCode.OK, shown.output
    assert f"id: {doc['id']}" in shown.stdout
    assert f"  QUESTA_HOME={top}" in shown.stdout
    by_id = json.loads(site.invoke(["toolchain", "show", doc["id"], "--json"]).stdout)
    assert by_id["toolchain"]["module"] == "questa/2025.2"

    again = site.invoke(["toolchain", "register", "questa/2025.2", "--root", str(uvm)])
    assert again.exit_code == ExitCode.OK, again.output
    assert again.stdout.startswith("unchanged questa/2025.2")
    assert "warning" not in again.stderr


# R2
def test_register_drift_warning(site: Site) -> None:
    mods = _modules(site)
    top = questa_like(mods)
    site.invoke(["toolchain", "register", "questa/2025.2"])
    (top / "bin" / "run").write_text("#!/bin/sh\necho patched\n")
    result = site.invoke(["toolchain", "register", "questa/2025.2"])
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout.startswith("registered questa/2025.2")
    assert "ebs: warning: toolchain questa/2025.2 changed" in result.stderr
    assert f"changed: {top}/bin/run" in result.stderr
    assert (
        len(json.loads(site.invoke(["toolchain", "list", "--all", "--json"]).stdout)["toolchains"])
        == 2
    )


# R4
def test_check_exit_codes(site: Site) -> None:
    mods = _modules(site)
    top = questa_like(mods)
    site.invoke(["toolchain", "register", "questa/2025.2"])
    ok = site.invoke(["toolchain", "check"])
    assert ok.exit_code == ExitCode.OK, ok.output
    assert ok.stdout.startswith("ok ")
    (top / "lib" / "libsim.so").write_text("patched")
    drift = site.invoke(["toolchain", "check", "--json"])
    assert drift.exit_code == ExitCode.ACTIONS_FAILED, drift.output
    (result,) = json.loads(drift.stdout)["results"]
    assert result["status"] == "drift"
    assert result["changes"]["files"] == [{"path": f"{top}/lib/libsim.so", "change": "changed"}]
    assert len(site.services.memory_store.list_toolchains()) == 1  # nothing registered


# R1
def test_errors(site: Site) -> None:
    result = site.invoke(["toolchain", "register", "questa/2025.2"])
    assert result.exit_code == ExitCode.USAGE
    assert "MODULESHOME" in result.stderr
    _modules(site)
    missing = site.invoke(["toolchain", "show", "nosuch/1.0"])
    assert missing.exit_code == ExitCode.USAGE
    assert "no registered toolchain" in missing.stderr
    bad = site.invoke(["toolchain", "show", "sha256:xyz"])
    assert bad.exit_code == ExitCode.USAGE


# R3
def test_planning_uses_registry_without_toolchains_yaml(site: Site) -> None:
    resolver = site.services.toolchains(site.proj)
    assert isinstance(resolver, RegistryToolchainResolver)


# R6
def test_register_rejects_user_name(site: Site) -> None:
    mods = _modules(site)
    top = questa_like(mods)
    mods.modulefile("questa/2025.2", f"setenv QUESTA_HOME {top}", "setenv OWNER /scratch/alice")
    result = site.invoke(["toolchain", "register", "questa/2025.2"])
    assert result.exit_code == ExitCode.USAGE
    assert "alice" in result.stderr
    assert site.services.memory_store.list_toolchains() == []


# R4: a toolchain that cannot be fingerprinted fails the nightly check too
def test_check_error_exit_code(site: Site) -> None:
    mods = _modules(site)
    top = questa_like(mods)
    site.invoke(["toolchain", "register", "questa/2025.2"])
    shutil.rmtree(top)
    result = site.invoke(["toolchain", "check", "--json"])
    assert result.exit_code == ExitCode.ACTIONS_FAILED
    assert json.loads(result.stdout)["results"][0]["status"] == "error"
