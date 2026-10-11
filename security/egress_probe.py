#!/usr/bin/env python3
"""Dual-channel helper for connected-IP egress policy attestation."""

from __future__ import annotations

# The health check runs every two seconds under the probe's quarter-CPU quota,
# in an image that ships no bytecode and on a read-only filesystem, so every
# Python module it imports is compiled from source each time: asyncio takes
# seconds there, and socket.py half a second. The check therefore uses only
# modules built into the interpreter (_socket is the C module under socket.py),
# and what the server needs is imported when the server starts.
import _socket
import os
import sys

REQUEST = "SCRAPEYARD-EGRESS-PROBE/1"
RESPONSE = "SCRAPEYARD-EGRESS-LIVE/1"
USAGE = (
    "usage: egress_probe.py [--healthcheck] [--host ADDRESS] "
    "[--challenge-port PORT] [--liveness-port PORT]"
)


def _serve(host: str, challenge_port: int, liveness_port: int) -> None:
    import asyncio
    from contextlib import suppress

    async def close_writer(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()

    async def serve() -> None:
        async def challenge(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await close_writer(writer)

        challenge_server = await asyncio.start_server(
            challenge,
            host,
            challenge_port,
            limit=1024,
        )

        async def liveness(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request = (await asyncio.wait_for(reader.readline(), timeout=2)).decode(
                    "ascii"
                ).rstrip("\n")
                prefix, nonce = request.split(" ", 1)
                if (
                    prefix == REQUEST
                    and len(nonce) == 32
                    and challenge_server.is_serving()
                ):
                    writer.write(f"{RESPONSE} {nonce} {challenge_port}\n".encode())
                    await writer.drain()
            except (TimeoutError, UnicodeDecodeError, ValueError):
                pass
            finally:
                await close_writer(writer)

        try:
            liveness_server = await asyncio.start_server(
                liveness,
                host,
                liveness_port,
                limit=1024,
            )
        except BaseException:
            challenge_server.close()
            await challenge_server.wait_closed()
            raise

        async with challenge_server, liveness_server:
            await asyncio.gather(
                challenge_server.serve_forever(),
                liveness_server.serve_forever(),
            )

    asyncio.run(serve())


def _healthcheck(host: str, challenge_port: int, liveness_port: int) -> None:
    nonce = os.urandom(16).hex()
    expected = f"{RESPONSE} {nonce} {challenge_port}\n".encode()
    connection = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        connection.settimeout(1)
        connection.connect((host, liveness_port))
        connection.sendall(f"{REQUEST} {nonce}\n".encode())
        response = b""
        while len(response) < len(expected):
            received = connection.recv(len(expected) - len(response))
            if not received:
                break
            response += received
    finally:
        connection.close()
    if response != expected:
        raise RuntimeError("egress probe liveness check failed")


def _arguments(arguments: list[str]) -> tuple[bool, str, int, int]:
    healthcheck = False
    host = "0.0.0.0"
    ports = {"--challenge-port": 8080, "--liveness-port": 8081}
    position = 0
    while position < len(arguments):
        name = arguments[position]
        if name == "--healthcheck":
            healthcheck = True
            position += 1
            continue
        if name not in ("--host", *ports) or position + 1 == len(arguments):
            raise SystemExit(USAGE)
        value = arguments[position + 1]
        if name == "--host":
            host = value
        elif value.isascii() and value.isdigit() and 0 < int(value) < 65536:
            ports[name] = int(value)
        else:
            raise SystemExit(f"{name} must be a port number\n{USAGE}")
        position += 2
    return healthcheck, host, ports["--challenge-port"], ports["--liveness-port"]


def main() -> None:
    healthcheck, host, challenge_port, liveness_port = _arguments(sys.argv[1:])
    if healthcheck:
        _healthcheck("127.0.0.1", challenge_port, liveness_port)
        return
    _serve(host, challenge_port, liveness_port)


if __name__ == "__main__":
    main()
