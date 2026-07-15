#!/usr/bin/env python3
"""Dual-channel helper for connected-IP egress policy attestation."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import secrets
import socket


REQUEST = "SCRAPEYARD-EGRESS-PROBE/1"
RESPONSE = "SCRAPEYARD-EGRESS-LIVE/1"


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(ConnectionError, OSError):
        await writer.wait_closed()


async def _serve(host: str, challenge_port: int, liveness_port: int) -> None:
    async def challenge(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _close_writer(writer)

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
            await _close_writer(writer)

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


def _healthcheck(host: str, challenge_port: int, liveness_port: int) -> None:
    nonce = secrets.token_hex(16)
    expected = f"{RESPONSE} {nonce} {challenge_port}\n".encode()
    with socket.create_connection((host, liveness_port), timeout=1) as connection:
        connection.sendall(f"{REQUEST} {nonce}\n".encode())
        response = connection.makefile("rb").readline(1024)
    if response != expected:
        raise RuntimeError("egress probe liveness check failed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--challenge-port", type=int, default=8080)
    parser.add_argument("--liveness-port", type=int, default=8081)
    parser.add_argument("--healthcheck", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.healthcheck:
        _healthcheck("127.0.0.1", args.challenge_port, args.liveness_port)
        return
    asyncio.run(_serve(args.host, args.challenge_port, args.liveness_port))


if __name__ == "__main__":
    main()
