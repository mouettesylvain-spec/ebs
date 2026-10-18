"""Describe the fakesim tool tree as a toolchain in .ebs/toolchains.yaml (site: Custom toolchains).

A toolchain's PATH must be absolute, and this example can live anywhere, so this script writes
the file with the absolute path of tools/fakesim-1.0. On a site with environment modules, an
admin runs `ebs toolchain register fakesim/1.0` instead.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "tools" / "fakesim-1.0"

TOOLCHAINS = f"""\
version: 1
toolchains:
  fakesim/1.0:                       # the module name the flow refers to
    version: "1.0"
    install_roots: [../tools/fakesim-1.0]   # fingerprinted; relative to this file
    env:                             # the tool's whole environment: nothing else leaks in
      PATH: {json.dumps(f"{ROOT}/bin:/usr/bin:/bin")}
      FAKESIM_HOME: {json.dumps(str(ROOT))}
"""

target = HERE / ".ebs" / "toolchains.yaml"
target.parent.mkdir(exist_ok=True)
target.write_text(TOOLCHAINS, encoding="utf-8")
print(f"wrote .ebs/toolchains.yaml (fakesim/1.0 in {ROOT})")
