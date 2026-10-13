from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from scrapeyard.runtime import healthcheck

_MONITOR = "monitor-secret-0123456789"
_CLIENT = "client-secret-0123456789"
# The client's credential comes first: the check has to choose by scope.
_CREDENTIALS = json.dumps(
    {
        "client": {"secret": _CLIENT, "scopes": ["submit", "read"], "projects": ["p"]},
        "monitor": {"secret": _MONITOR, "scopes": ["health-detail"]},
    }
)


class _Application(ThreadingHTTPServer):
    """Answers as the application does: readiness by key, liveness to anyone."""

    ready = True
    asked: list[str]


class _Handler(BaseHTTPRequestHandler):
    server: _Application

    def do_GET(self) -> None:
        self.server.asked.append(self.path)
        if self.path == "/health/live":
            status = 200
        elif self.path != "/health/ready":
            status = 404
        elif self.headers.get("X-API-Key") == _CLIENT:
            status = 403
        elif self.headers.get("X-API-Key") != _MONITOR:
            status = 401
        else:
            status = 200 if self.server.ready else 503
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_arguments: object) -> None:
        pass


@pytest.fixture
def application() -> Iterator[_Application]:
    server = _Application(("127.0.0.1", 0), _Handler)
    server.asked = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _check(application: _Application, environment: dict[str, str]) -> bool:
    return healthcheck.check(environment, port=application.server_address[1])


def test_healthy_only_while_the_application_reports_ready(application: _Application) -> None:
    environment = {"SCRAPEYARD_API_CREDENTIALS": _CREDENTIALS}

    assert _check(application, environment) is True
    application.ready = False
    # The process still answers liveness here, which the old check took for health.
    assert _check(application, environment) is False
    assert application.asked == ["/health/ready", "/health/ready"]


def test_it_asks_with_the_credential_that_holds_the_fewest_scopes(application: _Application) -> None:
    # A consumer's credential may hold the scope too. Readiness is rate-limited
    # by credential, so the check must not spend, or wait on, the consumer's.
    consumer_first = json.dumps(
        {
            "client": {"secret": _CLIENT, "scopes": ["submit", "read", "health-detail"]},
            "monitor": {"secret": _MONITOR, "scopes": ["health-detail"]},
        }
    )

    assert healthcheck.readiness_key({"SCRAPEYARD_API_CREDENTIALS": consumer_first}) == _MONITOR
    # Named credentials before a key from before them, which holds every scope.
    assert (
        healthcheck.readiness_key(
            {"SCRAPEYARD_API_CREDENTIALS": consumer_first, "SCRAPEYARD_API_KEYS": "legacy-key-0123456789"}
        )
        == _MONITOR
    )
    assert _check(application, {"SCRAPEYARD_API_CREDENTIALS": consumer_first}) is True


def test_a_key_no_header_can_carry_is_unhealthy_and_is_not_printed(
    application: _Application, capfd: pytest.CaptureFixture[str]
) -> None:
    assert _check(application, {"SCRAPEYARD_API_KEYS": "secret-with-a\nnewline-0123"}) is False
    assert "secret-with-a" not in "".join(capfd.readouterr())


def test_without_a_health_detail_credential_it_checks_liveness(
    application: _Application,
) -> None:
    client_only = json.dumps({"client": {"secret": _CLIENT, "scopes": ["submit", "read"]}})
    application.ready = False

    assert _check(application, {"SCRAPEYARD_API_CREDENTIALS": client_only}) is True
    assert _check(application, {}) is True
    assert application.asked == ["/health/live", "/health/live"]


def test_a_key_from_before_named_credentials_holds_the_scope(application: _Application) -> None:
    assert _check(application, {"SCRAPEYARD_API_KEYS": f" {_MONITOR} , other-key"}) is True
    assert application.asked == ["/health/ready"]


def test_unhealthy_when_nothing_answers_or_the_answer_never_comes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setattr(healthcheck, "_TIMEOUT_SECONDS", 0.2)
    try:
        # Accepted by the kernel and never answered.
        assert healthcheck.check({}, port=port) is False
    finally:
        listener.close()
    assert healthcheck.check({}, port=port) is False


@pytest.mark.parametrize(
    "credentials",
    ["not json", "[]", json.dumps({"monitor": "a-string"}), json.dumps({"monitor": {"scopes": 1}})],
)
def test_settings_the_application_would_refuse_fall_back_to_liveness(credentials: str) -> None:
    assert healthcheck.readiness_key({"SCRAPEYARD_API_CREDENTIALS": credentials}) is None


def test_compose_and_the_image_run_the_readiness_check() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    check = compose["services"]["scrapeyard"]["healthcheck"]
    assert check["test"] == ["CMD", "python", "-m", "scrapeyard.runtime.healthcheck"]
    # Docker's deadline for the whole check is the one that decides.
    assert int(check["timeout"].removesuffix("s")) > healthcheck._TIMEOUT_SECONDS
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "scrapeyard.runtime.healthcheck"]' in dockerfile
    assert "HEALTHCHECK --interval=30s --timeout=10s" in dockerfile
