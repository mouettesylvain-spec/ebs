"""Register a real Questa module (P1-07 R1); nightly on a node with the site's module system.

Set `EBS_VENDOR_QUESTA_MODULE` to the module to snapshot (e.g. `questa/2025.2`) in a shell where
`module` works (MODULESHOME and MODULEPATH set). Optional `EBS_VENDOR_QUESTA_ROOTS`: extra install
roots, separated by `:`.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path

import pytest

from ebs.core.clock import FakeClock
from ebs.meta.memory import InMemoryMetadataStore
from ebs.toolchain.modules import detect_module_system, snapshot_module
from ebs.toolchain.registry import RegistryToolchainResolver, register


# R1
def test_register_questa() -> None:
    module = os.environ.get("EBS_VENDOR_QUESTA_MODULE")
    if not module or not os.environ.get("MODULESHOME"):
        pytest.skip(
            "missing dependency: a Questa module (set EBS_VENDOR_QUESTA_MODULE=<name>/<version> "
            "in a shell where `module` works)"
        )
    extra = [Path(p) for p in os.environ.get("EBS_VENDOR_QUESTA_ROOTS", "").split(":") if p]
    user = getpass.getuser()
    system = detect_module_system(os.environ)
    snap = snapshot_module(module, system=system, environ=os.environ, user=user, extra_roots=extra)
    assert snap.install_roots
    assert "PATH" in snap.env
    assert not any(user in v.split("/") for v in snap.env.values())

    store = InMemoryMetadataStore(clock=FakeClock())
    first = register(
        store,
        module=snap.module,
        version=snap.version,
        env=snap.env,
        install_roots=snap.install_roots,
        user=user,
    )
    again = register(
        store,
        module=snap.module,
        version=snap.version,
        env=snap.env,
        install_roots=snap.install_roots,
        user=user,
    )
    assert again.status == "unchanged"
    assert again.toolchain.id == first.toolchain.id
    resolved = RegistryToolchainResolver(store).resolve("questa", module)
    assert resolved.id == first.toolchain.id
