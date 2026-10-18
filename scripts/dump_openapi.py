"""Write the metadata service's OpenAPI document, from which the dashboard's API types are
generated (`cd web && npm run gen:api`, P2-06).

    uv run python scripts/dump_openapi.py web/openapi.json

The document depends only on the routes and models, so the app is built on an in-memory store.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.app import create_app
from ebs.meta.service.auth import ChainAuthenticator
from ebs.meta.service.deps import ServiceDeps


class _Ready:
    def check(self) -> str | None:
        return None


def openapi() -> dict[str, object]:
    deps = ServiceDeps(
        store=InMemoryMetadataStore(), authenticator=ChainAuthenticator(), readiness=_Ready()
    )
    return create_app(deps).openapi()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: dump_openapi.py OUT.json\n")
        return 2
    text = json.dumps(openapi(), indent=2, sort_keys=True) + "\n"
    Path(argv[1]).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
