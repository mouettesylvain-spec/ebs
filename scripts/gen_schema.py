"""Write (or check) the committed flow JSON Schema, schemas/flow.v1.schema.json.

Usage: python scripts/gen_schema.py [--check] [--output PATH]

`--check` exits 1 without writing when the file differs from what the models generate.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ebs.flow.schema import render_schema

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "schemas" / "flow.v1.schema.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    output: Path = args.output
    expected = render_schema()
    if args.check:
        current = output.read_text(encoding="utf-8") if output.exists() else None
        if current != expected:
            sys.stderr.write(
                f"{output} is stale: run `uv run python scripts/gen_schema.py` and commit it\n"
            )
            return 1
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
