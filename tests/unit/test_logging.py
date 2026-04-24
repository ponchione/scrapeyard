"""Tests for scrapeyard.common.logging — JSON formatter correctness."""

import json
import logging

from scrapeyard.common.logging import _JsonFormatter


def _format_message(message: str, **kwargs) -> str:
    """Create a log record and format it with _JsonFormatter."""
    fmt = _JsonFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for k, v in kwargs.items():
        setattr(record, k, v)
    return fmt.format(record)


def test_plain_message_produces_valid_json():
    raw = _format_message("hello world")
    parsed = json.loads(raw)
    assert parsed["message"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.logger"
    assert "time" in parsed


def test_message_with_double_quotes():
    raw = _format_message('key is "value"')
    parsed = json.loads(raw)
    assert parsed["message"] == 'key is "value"'


def test_message_with_backslashes():
    raw = _format_message("path\\to\\file")
    parsed = json.loads(raw)
    assert parsed["message"] == "path\\to\\file"


def test_message_with_newlines():
    raw = _format_message("line1\nline2\ttab")
    parsed = json.loads(raw)
    assert parsed["message"] == "line1\nline2\ttab"


def test_message_with_unicode():
    raw = _format_message("prix: 42€ — résultat")
    parsed = json.loads(raw)
    assert "42€" in parsed["message"]


def test_message_with_exc_info():
    import sys

    fmt = _JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="failed",
            args=(),
            exc_info=exc_info,
        )
    raw = fmt.format(record)
    parsed = json.loads(raw)
    assert "ValueError: boom" in parsed["message"]
    assert parsed["message"].startswith("failed\n")
