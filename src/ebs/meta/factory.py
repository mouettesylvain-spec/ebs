"""Open the metadata store named by `[metadata].url` (P1-06 R7).

An `http(s)://` URL is the metadata service (`HttpMetadataStore`): what runners and CLIs use on
the cluster, without database credentials. A `postgresql…://` URL opens the database directly,
for development and for the service itself. Each backend is imported only when selected, so a
runner talking HTTP never loads SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import Mapping

from ebs.core.clock import Clock
from ebs.core.errors import ConfigError
from ebs.meta.api import MetadataStore

__all__ = ["open_metadata_store"]


def open_metadata_store(
    section: Mapping[str, object],
    *,
    clock: Clock | None = None,
    environ: Mapping[str, str] | None = None,
) -> MetadataStore:
    url = section.get("url")
    if not isinstance(url, str) or not url:
        raise ConfigError("[metadata].url must be set to the metadata service or database URL")
    scheme = url.partition(":")[0].lower()
    if scheme in ("http", "https"):
        from ebs.meta.client import HttpMetadataStore

        return HttpMetadataStore.from_config(section, environ=environ)
    if scheme == "postgresql" or scheme.startswith("postgresql+"):
        from ebs.meta.pg import PgMetadataStore

        return PgMetadataStore.from_config(section, clock=clock)
    raise ConfigError(
        f"[metadata].url has an unsupported scheme {scheme!r}: use the metadata service "
        "(https://ebs-meta.example) or, for development, the database "
        "(postgresql+psycopg://user@host/db)"
    )
