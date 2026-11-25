"""Bounded hostname-isolated httpx client cache for webhook delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from scrapeyard.common.async_tools import (
    AwaitableCancelled,
    MonotonicDeadline,
    await_cleanup,
    shield,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ClientCacheEntry:
    """One hostname-isolated client and its eviction-safety state."""

    client: httpx.AsyncClient
    last_used_at: float
    in_flight: int = 0
    expiry_task: asyncio.Task[None] | None = None


class WebhookClientPool:
    """LRU-bounded, idle-expiring pool of hostname-isolated httpx clients."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        max_size: int = 64,
        idle_ttl_seconds: float = 300.0,
    ) -> None:
        if max_size < 1:
            raise ValueError("client_cache_max_size must be positive")
        if idle_ttl_seconds <= 0:
            raise ValueError("client_cache_idle_ttl_seconds must be positive")
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(trust_env=False)
        )
        self._clients: dict[str, _ClientCacheEntry] = {}
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        self._max_size = max_size
        self._idle_ttl_seconds = idle_ttl_seconds
        self._close_task: asyncio.Task[None] | None = None

    @property
    def entries(self) -> dict[str, _ClientCacheEntry]:
        return self._clients

    @property
    def size(self) -> int:
        return len(self._clients)

    @property
    def close_task(self) -> asyncio.Task[None] | None:
        return self._close_task

    @property
    def close_pending(self) -> bool:
        return self._close_task is not None and not self._close_task.done()

    async def acquire(self, logical_hostname: str) -> httpx.AsyncClient:
        """Lease a hostname-isolated client without exceeding the cache bound."""

        evicted_client: httpx.AsyncClient | None = None
        client: httpx.AsyncClient | None = None
        try:
            while True:
                async with self._condition:
                    now = time.monotonic()
                    entry = self._clients.get(logical_hostname)
                    if entry is not None:
                        if entry.expiry_task is not None:
                            entry.expiry_task.cancel()
                            entry.expiry_task = None
                        entry.in_flight += 1
                        entry.last_used_at = now
                        client = entry.client
                        break

                    if len(self._clients) >= self._max_size:
                        idle = [
                            (candidate.last_used_at, hostname, candidate)
                            for hostname, candidate in self._clients.items()
                            if candidate.in_flight == 0
                        ]
                        if not idle:
                            await self._condition.wait()
                            continue
                        _last_used, evicted_hostname, evicted = min(idle)
                        self._clients.pop(evicted_hostname, None)
                        if evicted.expiry_task is not None:
                            evicted.expiry_task.cancel()
                        evicted_client = evicted.client

                    try:
                        created_client = self._client_factory()
                        if created_client is evicted_client:
                            raise RuntimeError(
                                "Webhook client_factory must return a distinct client "
                                "for each logical hostname"
                            )
                        if any(
                            created_client is existing.client
                            for existing in self._clients.values()
                        ):
                            raise RuntimeError(
                                "Webhook client_factory must return a distinct client "
                                "for each logical hostname"
                            )
                    except BaseException:
                        if evicted_client is not None:
                            self._condition.notify_all()
                        raise
                    client = created_client
                    self._clients[logical_hostname] = _ClientCacheEntry(
                        client=client,
                        last_used_at=now,
                        in_flight=1,
                    )
                    break
        except BaseException:
            if evicted_client is not None:
                try:
                    await await_cleanup(evicted_client.aclose())
                except Exception:
                    logger.exception(
                        "Failed to close evicted webhook client hostname=%s",
                        logical_hostname,
                    )
            raise

        assert client is not None
        if evicted_client is not None:
            try:
                await shield(evicted_client.aclose())
            except asyncio.CancelledError:
                await self.release(logical_hostname, client)
                raise
            except Exception:
                logger.exception(
                    "Failed to close evicted webhook client hostname=%s",
                    logical_hostname,
                )
        return client

    async def release(
        self,
        logical_hostname: str,
        client: httpx.AsyncClient,
    ) -> None:
        """Return a leased client and arm its idle expiration timer."""

        async with self._condition:
            entry = self._clients.get(logical_hostname)
            if entry is None or entry.client is not client:
                return
            entry.in_flight = max(0, entry.in_flight - 1)
            entry.last_used_at = time.monotonic()
            if entry.in_flight == 0:
                entry.expiry_task = asyncio.create_task(
                    self._expire_idle_client(logical_hostname, entry),
                    name="scrapeyard-webhook-client-expiry",
                )
                self._condition.notify_all()

    async def _expire_idle_client(
        self,
        logical_hostname: str,
        entry: _ClientCacheEntry,
    ) -> None:
        try:
            await asyncio.sleep(self._idle_ttl_seconds)
            async with self._condition:
                current = self._clients.get(logical_hostname)
                if current is not entry or entry.in_flight != 0:
                    return
                self._clients.pop(logical_hostname, None)
                entry.expiry_task = None
                self._condition.notify_all()
            await entry.client.aclose()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "Failed to close expired webhook client hostname=%s",
                logical_hostname,
            )

    async def close(self, deadline: MonotonicDeadline) -> bool:
        """Close all cached clients within *deadline*; return False on timeout."""

        async def _close_clients() -> None:
            async with self._condition:
                entries = list(self._clients.values())
                self._clients.clear()
                self._condition.notify_all()
            for entry in entries:
                if entry.expiry_task is not None:
                    entry.expiry_task.cancel()
            clients = list({id(entry.client): entry.client for entry in entries}.values())
            first_failure: Exception | None = None
            for client in clients:
                while True:
                    try:
                        await client.aclose()
                        break
                    except asyncio.CancelledError:
                        # Closing is idempotent. Keep this owned task visible
                        # until the client acknowledges closure, even after the
                        # outer hard deadline requests cancellation.
                        continue
                    except Exception as exc:
                        if first_failure is None:
                            first_failure = exc
                        break
            if first_failure is not None:
                raise first_failure

        if self._close_task is None:
            self._close_task = asyncio.create_task(
                _close_clients(),
                name="scrapeyard-webhook-client-close",
            )

        try:
            await deadline.run(self._close_task)
        except AwaitableCancelled:
            return True
        except TimeoutError:
            return False
        finally:
            if self._close_task.done():
                self._close_task = None
        return True
