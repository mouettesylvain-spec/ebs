"""Sandbox v2: bubblewrap (architecture.md § "Sandboxing phases"; task P3-01 R2).

Every namespace is unshared except the network (FlexLM license checkouts need it). The tool sees
the OS directories read-only, a small subset of `/etc`, a private `/tmp`, fresh `/proc` and a
minimal `/dev`, then the runner's mounts: toolchain install roots and the domain's CAS read-only,
the action's scratch read-write. Nothing else exists, so `$HOME/.synopsys_dc.setup` or a project
NFS path fails with ENOENT. The env is exactly the runner's (`--clearenv`, then `--setenv`).
`--die-with-parent` takes the tool down if the runner dies.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ebs.sandbox.api import Mount, check_path

__all__ = ["BWRAP_NAMESPACES", "ETC_FILES", "SYSTEM_DIRS", "BwrapSandbox"]

BWRAP_NAMESPACES: Final = ("--unshare-all", "--share-net", "--die-with-parent")
SYSTEM_DIRS: Final = ("/lib", "/lib32", "/lib64", "/libx32", "/bin", "/sbin")
"""Bound read-only if present (besides `/usr`, which must exist); merged-/usr links included."""
ETC_FILES: Final = (
    "/etc/passwd",
    "/etc/group",
    "/etc/hosts",
    "/etc/resolv.conf",
    "/etc/nsswitch.conf",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/alternatives",
    "/etc/localtime",
    "/var/lib/sss/pipes",
    "/run/nscd",
)
"""User/group names (incl. the SSSD and nscd sockets of LDAP sites), name resolution (license
servers), the dynamic linker, `/usr/bin` links into alternatives and the time zone; nothing else."""


@dataclass(frozen=True, slots=True)
class BwrapSandbox:
    binary: str  # absolute path of `bwrap` on this node

    @property
    def name(self) -> str:
        return "bwrap"

    def wrap(
        self, argv: Sequence[str], mounts: Sequence[Mount], env: Mapping[str, str], cwd: str
    ) -> list[str]:
        out = [self.binary, *BWRAP_NAMESPACES, "--clearenv", "--ro-bind", "/usr", "/usr"]
        for path in (*SYSTEM_DIRS, *ETC_FILES):
            out += ["--ro-bind-try", path, path]
        out += ["--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev"]
        for m in mounts:
            src, dst = check_path(m.src, "mount source"), check_path(m.dst, "mount target")
            out += ["--bind" if m.writable else "--ro-bind", src, dst]
        for name in sorted(env):
            out += ["--setenv", name, env[name]]
        out += ["--chdir", check_path(cwd, "work directory"), "--", *argv]
        return out

    def setup_error(self, exit_code: int, log_head: str) -> str | None:
        # bwrap reports its own failures (missing mount source, no user namespaces, exec of a
        # program not in the sandbox) as "bwrap: ..." and exits 1 before the tool writes anything.
        first = log_head.split("\n", 1)[0].strip()
        return first if exit_code == 1 and first.startswith("bwrap: ") else None
