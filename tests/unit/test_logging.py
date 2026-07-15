"""Tests for scrapeyard.common.logging — JSON formatter correctness."""

import json
import io
import logging

from scrapling.engines.toolbelt.custom import Response

from scrapeyard.common.logging import _JsonFormatter, setup_logging
from scrapeyard.engine.url_guard import (
    activate_deployment_secret_redaction,
    reset_deployment_secret_redaction,
)


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


def test_formatter_redacts_run_scoped_secret_from_exception_traceback():
    import sys

    secret = "formatter-secret-sentinel"
    token = activate_deployment_secret_redaction((secret,))
    try:
        try:
            raise ValueError(secret)
        except ValueError:
            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname="test.py",
                lineno=1,
                msg="failed",
                args=(),
                exc_info=sys.exc_info(),
            )
        parsed = json.loads(_JsonFormatter().format(record))
        assert secret not in parsed["message"]
        assert "ValueError: <redacted>" in parsed["message"]
    finally:
        reset_deployment_secret_redaction(token)


def test_setup_logging_removes_scrapling_plaintext_handler_and_redacts_once(
    tmp_path,
    capsys,
):
    root = logging.getLogger()
    scrapling_logger = logging.getLogger("scrapling")
    original_root_handlers = list(root.handlers)
    original_scrapling_handlers = list(scrapling_logger.handlers)
    original_propagate = scrapling_logger.propagate
    original_initialized = getattr(root, "_scrapeyard_logging_initialized", None)
    plaintext = io.StringIO()
    try:
        root.handlers.clear()
        if hasattr(root, "_scrapeyard_logging_initialized"):
            delattr(root, "_scrapeyard_logging_initialized")
        dependency_handler = logging.StreamHandler(plaintext)
        scrapling_logger.handlers[:] = [dependency_handler]
        scrapling_logger.propagate = True

        setup_logging(str(tmp_path))
        Response(
            url="https://user:url-secret@example.com/?token=query-secret",
            text="<html></html>",
            body=b"<html></html>",
            status=200,
            reason="OK",
            cookies={},
            headers={},
            request_headers={
                "referer": "https://ref:referrer-secret@example.net/path"
            },
        )

        stderr = capsys.readouterr().err
        file_log = (tmp_path / "scrapeyard.log").read_text()
        combined = stderr + file_log
        assert "url-secret" not in combined
        assert "query-secret" not in combined
        assert "referrer-secret" not in combined
        assert len(stderr.splitlines()) == 1
        assert len(file_log.splitlines()) == 1
        assert plaintext.getvalue() == ""
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers[:] = original_root_handlers
        scrapling_logger.handlers[:] = original_scrapling_handlers
        scrapling_logger.propagate = original_propagate
        if original_initialized is None:
            if hasattr(root, "_scrapeyard_logging_initialized"):
                delattr(root, "_scrapeyard_logging_initialized")
        else:
            root._scrapeyard_logging_initialized = original_initialized
