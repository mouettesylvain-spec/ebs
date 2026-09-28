from __future__ import annotations

import io
import json
import logging

import pytest

from ebs.core.errors import ConfigError
from ebs.core.log import configure_logging, get_logger


def test_json_output_has_event_logger_and_fields() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", json=True, stream=stream)
    get_logger("ebs.test").info("cache_hit", action="sim[test=a]")
    record = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert record["event"] == "cache_hit"
    assert record["action"] == "sim[test=a]"
    assert record["logger"] == "ebs.test"
    assert record["level"] == "info"
    assert "timestamp" in record


def test_level_filters_lower_records() -> None:
    stream = io.StringIO()
    configure_logging(level=logging.WARNING, json=True, stream=stream)
    log = get_logger("ebs.test")
    log.info("hidden")
    log.warning("shown")
    assert "hidden" not in stream.getvalue()
    assert "shown" in stream.getvalue()


def test_console_output_is_human_readable() -> None:
    stream = io.StringIO()
    configure_logging(level="DEBUG", json=False, stream=stream)
    get_logger("ebs.test").debug("planning", actions=3)
    out = stream.getvalue()
    assert "planning" in out
    assert "actions=3" in out


def test_bound_context_is_kept() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", json=True, stream=stream)
    get_logger("ebs.test").bind(build="b1").info("start")
    assert json.loads(stream.getvalue().strip())["build"] == "b1"


def test_logger_created_before_configuration_follows_it() -> None:
    log = get_logger("ebs.early")  # module-level loggers are created at import time
    stream = io.StringIO()
    configure_logging(level="INFO", json=True, stream=stream)
    log.info("late")
    assert json.loads(stream.getvalue().strip())["logger"] == "ebs.early"


def test_unnamed_structlog_logger_gets_no_logger_field() -> None:
    import structlog

    stream = io.StringIO()
    configure_logging(level="INFO", json=True, stream=stream)
    structlog.get_logger().info("anon")
    assert "logger" not in json.loads(stream.getvalue().strip())


def test_level_names_are_case_insensitive() -> None:
    stream = io.StringIO()
    configure_logging(level="warning", json=True, stream=stream)
    log = get_logger("ebs.test")
    log.info("hidden")
    log.warning("shown")
    assert "hidden" not in stream.getvalue()
    assert "shown" in stream.getvalue()


def test_unknown_level_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="unknown log level 'loud'"):
        configure_logging(level="loud")


def test_context_variables_are_merged() -> None:
    import structlog

    stream = io.StringIO()
    configure_logging(level="INFO", json=True, stream=stream)
    structlog.contextvars.bind_contextvars(build="b7")
    try:
        get_logger("ebs.test").info("step")
    finally:
        structlog.contextvars.clear_contextvars()
    assert json.loads(stream.getvalue().strip())["build"] == "b7"


def test_default_stream_is_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json=True)
    get_logger("ebs.test").info("to_stderr")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "to_stderr" in captured.err


def test_timestamps_are_utc() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", json=True, stream=stream)
    get_logger("ebs.test").info("t")
    assert json.loads(stream.getvalue().strip())["timestamp"].endswith("Z")


def test_reconfiguring_redirects_existing_loggers() -> None:
    first, second = io.StringIO(), io.StringIO()
    configure_logging(level="INFO", json=True, stream=first)
    log = get_logger("ebs.test")
    log.info("one")
    configure_logging(level="INFO", json=True, stream=second)
    log.info("two")
    assert "two" not in first.getvalue()
    assert "two" in second.getvalue()
