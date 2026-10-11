"""A fake module system on disk (P1-07): Environment Modules or Lmod, modulefiles and tool trees.

The command is `tests/fakes/modules/fake_modulecmd`, installed where the real one lives:
`$MODULESHOME/libexec/modulecmd.tcl` (Environment Modules 4/5) or `$MODULESHOME/libexec/lmod`.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FAKE_MODULECMD = Path(__file__).resolve().parents[1] / "fakes" / "modules" / "fake_modulecmd"

Flavour = Literal["envmodules", "lmod"]
COMMANDS: dict[Flavour, str] = {"envmodules": "modulecmd.tcl", "lmod": "lmod"}
VERSIONS: dict[Flavour, str] = {"envmodules": "5.4.0", "lmod": "8.7.37"}


@dataclass
class ModuleSite:
    root: Path
    flavour: Flavour

    @property
    def moduleshome(self) -> Path:
        return self.root / self.flavour

    @property
    def modulepath(self) -> Path:
        return self.root / "modulefiles"

    @property
    def tools(self) -> Path:
        return self.root / "tools"

    @property
    def command(self) -> Path:
        return self.moduleshome / "libexec" / COMMANDS[self.flavour]

    def environ(self, *, user: str = "alice", home: str = "/home/alice") -> dict[str, str]:
        """A user's login environment: module settings plus things that must not leak."""
        return {
            "MODULESHOME": str(self.moduleshome),
            "MODULEPATH": str(self.modulepath),
            "USER": user,
            "LOGNAME": user,
            "HOME": home,
            "PATH": f"{home}/bin:/usr/bin:/bin",
            "EBS_LEAK_CHECK": "from the caller",
        }

    def modulefile(self, module: str, *lines: str) -> Path:
        path = self.modulepath / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{line}\n" for line in lines))
        return path

    def tool(self, rel: str, *, files: dict[str, str] | None = None) -> Path:
        """A tool tree under `tools/<rel>` with an executable `bin/run` and the given files."""
        top = self.tools / rel
        (top / "bin").mkdir(parents=True, exist_ok=True)
        run = top / "bin" / "run"
        run.write_text("#!/bin/sh\necho run\n")
        run.chmod(0o755)
        for name, text in (files or {}).items():
            (top / name).parent.mkdir(parents=True, exist_ok=True)
            (top / name).write_text(text)
        return top

    def set_version(self, version: str) -> None:
        (self.command.parent / ".fake-version").write_text(version)

    def calls(self) -> list[str]:
        path = self.command.parent / ".calls"
        return path.read_text().splitlines() if path.exists() else []


def make_module_site(root: Path, flavour: Flavour) -> ModuleSite:
    site = ModuleSite(root, flavour)
    site.command.parent.mkdir(parents=True)
    shutil.copy2(FAKE_MODULECMD, site.command)
    (site.command.parent / ".fake-flavour").write_text(flavour)
    site.set_version(VERSIONS[flavour])
    site.modulepath.mkdir(parents=True)
    site.tools.mkdir(parents=True)
    return site


def questa_like(site: ModuleSite, version: str = "2025.2") -> Path:
    """A `questa/<version>` modulefile pointing at a fake tool tree; returns the tree."""
    top = site.tool(f"questa/{version}", files={"lib/libsim.so": "lib", "docs/readme.txt": "doc"})
    site.modulefile(
        f"questa/{version}",
        f"setenv QUESTA_HOME {top}",
        f"prepend-path PATH {top}/bin",
        f"prepend-path LD_LIBRARY_PATH {top}/lib",
        "setenv QUESTA_MODE batch",
    )
    return top
