"""FakeSlurm: SLURM client commands backed by local processes (docs/design/testing.md, P1-01).

`FakeSlurm.install(dir)` writes executable `sbatch`, `squeue`, `sacct`, `scancel` and `scontrol`
scripts into `dir/bin` (put that first on PATH, or pass it as `SlurmCli(bin_dir=…)`) and keeps the
cluster state in `dir/state`. See `core.py` for the configuration and fault-injection files.

Every fake command and every job supervisor imports this package, once per call or job, so it
loads the test-side API (`fake.py`, with its heavier imports) only when a test asks for it.
"""

from __future__ import annotations

TYPE_CHECKING = False
if TYPE_CHECKING:
    from tests.fakes.slurm.fake import COMMAND_NAMES, FakeSlurm

__all__ = ["COMMAND_NAMES", "FakeSlurm"]


def __getattr__(name: str) -> object:
    if name in __all__:
        from tests.fakes.slurm import fake

        return getattr(fake, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
