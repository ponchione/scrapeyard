from __future__ import annotations

import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_controlled_egress_probe_serves_challenge_and_liveness_protocol():
    script = Path("security/egress_probe.py")
    challenge_port = _unused_port()
    liveness_port = _unused_port()
    while liveness_port == challenge_port:
        liveness_port = _unused_port()
    command = [
        sys.executable,
        str(script),
        "--host",
        "127.0.0.1",
        "--challenge-port",
        str(challenge_port),
        "--liveness-port",
        str(liveness_port),
    ]
    healthcheck = command[0:2] + [
        "--healthcheck",
        "--challenge-port",
        str(challenge_port),
        "--liveness-port",
        str(liveness_port),
    ]
    with subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stderr is not None
        try:
            deadline = time.monotonic() + 2
            while True:
                checked = subprocess.run(healthcheck, capture_output=True, text=True)
                if checked.returncode == 0:
                    break
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read())
                if time.monotonic() >= deadline:
                    raise AssertionError(checked.stderr)
                time.sleep(0.02)

            with socket.create_connection(
                ("127.0.0.1", challenge_port), timeout=1
            ) as probe:
                assert probe.recv(1) == b""
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    assert process.returncode is not None
    assert process.stderr.closed


class _Liveness(socketserver.StreamRequestHandler):
    def handle(self):
        request = self.rfile.readline().decode().split()
        for part in self.server.answer(request):
            self.wfile.write(part.encode())
            self.wfile.flush()
            time.sleep(0.05)


def _check_against(answer, *python_options: str) -> tuple[subprocess.CompletedProcess, list, float]:
    """Runs the health check against a liveness server that answers as told."""
    challenge_port = _unused_port()
    requests: list[list[str]] = []
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Liveness) as server:
        server.daemon_threads = True
        server.answer = lambda request: requests.append(request) or answer(request, challenge_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            checked = subprocess.run(
                [sys.executable, *python_options, "security/egress_probe.py", "--healthcheck",
                 "--challenge-port", str(challenge_port), "--liveness-port", str(server.server_address[1])],
                capture_output=True,
                text=True,
            )
        finally:
            taken = time.monotonic() - started
            server.shutdown()
            thread.join(timeout=2)
    return checked, requests, taken


def _right(request, port):
    return [f"SCRAPEYARD-EGRESS-LIVE/1 {request[1]} {port}\n"]


def _imported(stderr: str) -> set[str]:
    return {
        line.rsplit("|", 1)[1].strip() for line in stderr.splitlines() if line.startswith("import time:")
    }


def test_the_health_check_compiles_no_module_it_can_do_without():
    # The probe's image has no bytecode and a read-only filesystem, so each
    # health check compiles whatever it imports, under a quarter of a CPU.
    # asyncio, argparse, secrets and socket.py cost 3.3 s, 0.7 s, 0.7 s and
    # 0.5 s of that on the first production host (2026-10-11).
    options = ("-I", "-S", "-X", "importtime")
    # What this interpreter loads for a script that imports the same three
    # modules and does nothing: a check that passes may load that and no more.
    idle = _imported(
        subprocess.run(
            [sys.executable, *options, "-c", "from __future__ import annotations; import _socket, os, sys"],
            capture_output=True,
            text=True,
        ).stderr
    )

    checked, _requests, _taken = _check_against(_right, *options)

    assert checked.returncode == 0, checked.stderr
    assert _imported(checked.stderr) - idle == set()
    assert "_socket" in idle and "socket" not in idle and "asyncio" not in idle


@pytest.mark.parametrize(
    ("answer", "healthy"),
    [
        (_right, True),
        (lambda request, port: ["SCRAPEYARD-EGRESS-LIVE/1 ", f"{request[1]} {port}\n"], True),
        (lambda request, port: [f"SCRAPEYARD-EGRESS-LIVE/1 {request[1]} {port + 1}\n"], False),
        (lambda request, port: [f"SCRAPEYARD-EGRESS-LIVE/1 {'0' * 32} {port}\n"], False),
        (lambda request, port: [f"SCRAPEYARD-EGRESS-LIVE/1 {request[1]} {port}"], False),
        (lambda request, port: [f"SCRAPEYARD-EGRESS-LIVE/1 {request[1]}"], False),
        (lambda request, port: [], False),
    ],
    ids=["right", "in-two-parts", "another-challenge-port", "another-nonce", "no-newline", "cut-short", "silent"],
)
def test_the_health_check_accepts_only_its_own_nonce_and_the_challenge_port(answer, healthy):
    checked, requests, _taken = _check_against(answer)

    assert (checked.returncode == 0) is healthy, checked.stderr
    assert len(requests) == 1 and requests[0][0] == "SCRAPEYARD-EGRESS-PROBE/1"
    assert len(requests[0][1]) == 32 and int(requests[0][1], 16) >= 0
    if not healthy:
        assert "egress probe liveness check failed" in checked.stderr


def test_the_health_check_gives_up_on_a_probe_that_does_not_answer():
    # Docker's own deadline for the check is three seconds.
    def never(request, port):
        time.sleep(3)
        return []

    checked, _requests, taken = _check_against(never)

    assert checked.returncode != 0
    assert taken < 2.5


def test_the_probe_refuses_arguments_it_does_not_know():
    for arguments in (["--liveness-port", "none"], ["--challenge-port"], ["--port", "1"], ["--liveness-port", "0"]):
        refused = subprocess.run(
            [sys.executable, "security/egress_probe.py", "--healthcheck", *arguments],
            capture_output=True,
            text=True,
        )
        assert refused.returncode == 1, arguments
        assert "usage: egress_probe.py" in refused.stderr
