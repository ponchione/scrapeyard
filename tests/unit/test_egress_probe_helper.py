from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path


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


def test_the_health_check_compiles_no_module_it_can_do_without():
    # The probe's image has no bytecode and a read-only filesystem, so each
    # health check compiles whatever it imports, under a quarter of a CPU.
    # asyncio, argparse, secrets and socket.py cost 3.3 s, 0.7 s, 0.7 s and
    # 0.5 s of that on the first production host (2026-10-11).
    traced = subprocess.run(
        [sys.executable, "-I", "-S", "-X", "importtime", "security/egress_probe.py",
         "--healthcheck", "--liveness-port", str(_unused_port())],
        capture_output=True,
        text=True,
    )

    # Nothing listens there, so the check has imported all it uses and failed.
    assert traced.returncode != 0 and "ConnectionRefusedError" in traced.stderr
    imported = {
        line.rsplit("|", 1)[1].strip()
        for line in traced.stderr.splitlines()
        if line.startswith("import time:")
    }
    assert "_socket" in imported
    assert not imported & {"asyncio", "argparse", "secrets", "socket", "selectors", "ssl"}


def test_the_probe_refuses_arguments_it_does_not_know():
    for arguments in (["--liveness-port", "none"], ["--challenge-port"], ["--port", "1"], ["--liveness-port", "0"]):
        refused = subprocess.run(
            [sys.executable, "security/egress_probe.py", "--healthcheck", *arguments],
            capture_output=True,
            text=True,
        )
        assert refused.returncode == 1, arguments
        assert "usage: egress_probe.py" in refused.stderr
