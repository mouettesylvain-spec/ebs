"""EBS, the EDA Build System: hash-based build orchestration for EDA flows on SLURM."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ebs")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0+unknown"

__all__ = ["__version__"]
