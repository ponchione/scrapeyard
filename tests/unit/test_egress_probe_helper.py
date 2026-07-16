from __future__ import annotations

from pathlib import Path
import socket
import subprocess
import sys
import time


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
