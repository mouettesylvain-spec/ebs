"""Fallback sandbox: Apptainer, for nodes without unprivileged user namespaces for bwrap (D6).

`exec --containall` gives the tool a container from a site image (`[sandbox].apptainer_image`,
an OS image matching the nodes) with its own /tmp, no home and none of the admin's bind paths;
only the runner's mounts are bound in, at the same paths. `--cleanenv` drops the host env and
`env -i` inside sets exactly the runner's, since Apptainer's own `--env` cannot carry values
containing commas.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ebs.core.errors import SandboxError
from ebs.sandbox.api import Mount, check_path

__all__ = ["ApptainerSandbox"]

_ENV: str = "/usr/bin/env"


@dataclass(frozen=True, slots=True)
class ApptainerSandbox:
    binary: str  # absolute path of `apptainer` on this node
    image: str  # the site's OS image (.sif or sandbox dir)

    @property
    def name(self) -> str:
        return "apptainer"

    def wrap(
        self, argv: Sequence[str], mounts: Sequence[Mount], env: Mapping[str, str], cwd: str
    ) -> list[str]:
        # `--no-mount bind-paths` (Apptainer >= 1.1): the admin's `bind path` entries (often
        # /proj or /home) would otherwise defeat the sandbox.
        out = [self.binary, "exec", "--containall", "--cleanenv", "--no-home"]
        out += ["--no-mount", "bind-paths"]
        for m in mounts:
            src, dst = check_path(m.src, "mount source"), check_path(m.dst, "mount target")
            for path in (src, dst):
                if ":" in path or "," in path:
                    raise SandboxError(
                        f"cannot bind {path!r} with apptainer: its bind syntax uses ':' and ','; "
                        "rename the directory or use the bwrap sandbox"
                    )
            out += ["--bind", f"{src}:{dst}:{'rw' if m.writable else 'ro'}"]
        if argv and "=" in argv[0]:
            raise SandboxError(f"cannot run {argv[0]!r} with apptainer: '=' in a program name")
        out += ["--pwd", check_path(cwd, "work directory"), self.image, _ENV, "-i"]
        out += [f"{name}={env[name]}" for name in sorted(env)]
        return [*out, *argv]

    def setup_error(self, exit_code: int, log_head: str) -> str | None:
        # Apptainer reports its own failures as "FATAL: ..." with exit code 255.
        first = log_head.split("\n", 1)[0].strip()
        return first if exit_code == 255 and first.startswith("FATAL:") else None
