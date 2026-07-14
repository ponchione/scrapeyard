"""Live-socket regression tests for bounded production basic fetches."""

from __future__ import annotations

import asyncio
import gzip
from contextlib import suppress

import pytest
from scrapling import Fetcher

from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.engine.basic_fetch import fetch_streaming_response


def _budget(*, fetched_bytes: int = 1024, duration: float = 5) -> RunBudget:
    return RunBudget(
        max_duration_seconds=duration,
        max_fetched_bytes=fetched_bytes,
        max_extracted_records=100,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=1024,
    )


async def _request_url(handler) -> tuple[asyncio.AbstractServer, str]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}/"


async def _read_request(reader: asyncio.StreamReader) -> None:
    await reader.readuntil(b"\r\n\r\n")


async def test_chunked_body_stops_at_live_byte_ceiling() -> None:
    connection_closed = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _read_request(reader)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            for chunk in (b"abc", b"def", b"ghi"):
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
                await asyncio.sleep(0)
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()
            connection_closed.set()

    server, url = await _request_url(handler)
    try:
        with pytest.raises(BudgetExceeded) as exc_info:
            await fetch_streaming_response(
                Fetcher,
                url,
                {"timeout": 2, "stealthy_headers": False},
                budget=_budget(fetched_bytes=5),
            )
        await asyncio.wait_for(connection_closed.wait(), timeout=1)
    finally:
        server.close()
        await server.wait_closed()

    assert exc_info.value.limit_name is BudgetLimitName.fetched_bytes
    assert exc_info.value.observed_amount > 5


async def test_oversized_content_length_is_rejected_before_body_read() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _read_request(reader)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n")
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()

    server, url = await _request_url(handler)
    budget = _budget(fetched_bytes=8)
    try:
        with pytest.raises(BudgetExceeded) as exc_info:
            await fetch_streaming_response(
                Fetcher,
                url,
                {"timeout": 2, "stealthy_headers": False},
                budget=budget,
            )
    finally:
        server.close()
        await server.wait_closed()

    assert exc_info.value.observed_amount == 999
    assert budget.fetched_bytes == 0


async def test_decoded_body_crosses_ceiling_despite_small_content_length() -> None:
    body = b"x" * 4096
    encoded = gzip.compress(body)
    assert len(encoded) < len(body)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _read_request(reader)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Encoding: gzip\r\n"
                + f"Content-Length: {len(encoded)}\r\n\r\n".encode()
                + encoded
            )
            await writer.drain()
        finally:
            writer.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()

    server, url = await _request_url(handler)
    try:
        with pytest.raises(BudgetExceeded) as exc_info:
            await fetch_streaming_response(
                Fetcher,
                url,
                {"timeout": 2, "stealthy_headers": False},
                budget=_budget(fetched_bytes=len(encoded) + 1),
            )
    finally:
        server.close()
        await server.wait_closed()

    assert exc_info.value.limit_name is BudgetLimitName.fetched_bytes
    assert exc_info.value.observed_amount == len(body)


async def test_slow_continuous_body_closes_socket_before_deadline_error_returns() -> None:
    connection_closed = asyncio.Event()
    chunks_sent = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal chunks_sent
        try:
            await _read_request(reader)
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            await writer.drain()
            for _ in range(100):
                writer.write(b"1\r\nx\r\n")
                await writer.drain()
                chunks_sent += 1
                await asyncio.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()
            connection_closed.set()

    server, url = await _request_url(handler)
    try:
        with pytest.raises(BudgetExceeded) as exc_info:
            await fetch_streaming_response(
                Fetcher,
                url,
                {"timeout": 1, "stealthy_headers": False},
                budget=_budget(duration=0.12),
            )
        await asyncio.wait_for(connection_closed.wait(), timeout=1)
        sent_at_return = chunks_sent
        await asyncio.sleep(0.1)
    finally:
        server.close()
        await server.wait_closed()

    assert exc_info.value.limit_name is BudgetLimitName.run_duration_seconds
    assert chunks_sent == sent_at_return
    assert chunks_sent < 100
