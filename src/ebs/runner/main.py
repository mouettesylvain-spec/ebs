"""Entry point of `ebs-runner`, the compute-node runner (implemented in P0-13)."""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    sys.stderr.write("ebs-runner: not implemented yet (task P0-13)\n")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
