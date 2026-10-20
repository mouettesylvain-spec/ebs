"""Fixtures for contract suites."""

from __future__ import annotations

import pytest

from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url
from tests.helpers.s3 import s3_server

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url", "s3_server"]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "fs_only: a CAS contract test that needs local paths (skipped for S3)"
    )
