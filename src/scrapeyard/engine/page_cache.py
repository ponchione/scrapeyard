"""Record/replay cache of rendered top-level pages (development aid).

``record`` stores every successfully fetched top-level page (initial,
pagination and click-pagination snapshots) as rendered HTML plus minimal
metadata. ``replay`` serves those pages without any network access so
selectors and pagination can be iterated without contacting the site.
Replayed output is not a live observation of the site.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from scrapeyard.common.paths import safe_path_part
from scrapeyard.config.schema import PageCacheMode
from scrapeyard.engine.scrape_models import ScrapeStop
from scrapeyard.engine.url_guard import redact_userinfo_in_url, url_host_label
from scrapeyard.models.job import ErrorType


class PageCacheMiss(ScrapeStop):
    """A replayed page was never recorded (or the cache is unavailable)."""

    error_type = ErrorType.cache_miss
    pagination_stop_reason = "cache_miss"

    def __init__(self, url: str, *, click_index: int = 0, reason: str | None = None) -> None:
        location = redact_userinfo_in_url(url)
        if click_index:
            location = f"{location} (click page {click_index + 1})"
        super().__init__(
            reason
            or f"Page cache miss for {location}; replay makes no requests"
        )
        self.url = url
        self.click_index = click_index


@dataclass(frozen=True)
class CachedPage:
    html: str
    final_url: str
    status: int
    content_type: str
    fetcher: str
    recorded_at: str
    click_pagination: dict[str, Any] | None = None


def canonical_cache_url(url: str) -> str:
    """Scheme/host/path/query identity of a page; fragments never select content."""
    parsed = urlsplit(url)
    return urlunsplit((
        parsed.scheme.lower(),
        url_host_label(url),
        parsed.path or "/",
        parsed.query,
        "",
    ))


@dataclass(frozen=True)
class PageCache:
    """Page cache for one project within ``SCRAPEYARD_PAGE_CACHE_DIR``."""

    mode: PageCacheMode
    root: Path

    @classmethod
    def for_project(cls, mode: PageCacheMode, cache_dir: str, project: str) -> PageCache:
        return cls(mode=mode, root=Path(cache_dir) / safe_path_part(project, label="project"))

    def _paths(self, url: str, fetcher: str, click_index: int) -> tuple[Path, Path]:
        key = f"{fetcher}\n{canonical_cache_url(url)}\n{click_index}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        directory = self.root / digest[:2]
        return directory / f"{digest}.html", directory / f"{digest}.json"

    def load(self, url: str, fetcher: str, *, click_index: int = 0) -> CachedPage | None:
        html_path, meta_path = self._paths(url, fetcher, click_index)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            html = html_path.read_text(encoding="utf-8")
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return CachedPage(
            html=html,
            final_url=str(meta.get("final_url") or url),
            status=int(meta.get("status") or 200),
            content_type=str(meta.get("content_type") or "text/html"),
            fetcher=str(meta.get("fetcher") or fetcher),
            recorded_at=str(meta.get("recorded_at") or ""),
            click_pagination=meta.get("click_pagination"),
        )

    def store(
        self,
        url: str,
        fetcher: str,
        *,
        html: str,
        final_url: str,
        status: int,
        content_type: str,
        click_index: int = 0,
        click_pagination: dict[str, Any] | None = None,
    ) -> None:
        html_path, meta_path = self._paths(url, fetcher, click_index)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "url": redact_userinfo_in_url(url),
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "fetcher": fetcher,
            "click_index": click_index,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if click_pagination is not None:
            meta["click_pagination"] = click_pagination
        _atomic_write(html_path, html)
        _atomic_write(meta_path, json.dumps(meta, sort_keys=True))


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class PageCacheUnavailable(PageCacheMiss):
    """The run asked for the page cache but the service has no cache directory."""

    def __init__(self, url: str) -> None:
        super().__init__(
            url,
            reason=(
                "Page cache requested but SCRAPEYARD_PAGE_CACHE_DIR is not configured; "
                "no request was made"
            ),
        )


_ACTIVE_PAGE_CACHE: ContextVar[PageCache | None] = ContextVar(
    "scrapeyard_active_page_cache",
    default=None,
)
# A run that asked for the cache while the service has none must not fall back
# to live fetching: replay promises zero requests.
_PAGE_CACHE_UNAVAILABLE: ContextVar[bool] = ContextVar(
    "scrapeyard_page_cache_unavailable",
    default=False,
)


@contextmanager
def activate_page_cache(
    cache: PageCache | None,
    *,
    unavailable: bool = False,
) -> Iterator[None]:
    cache_token = _ACTIVE_PAGE_CACHE.set(cache)
    unavailable_token = _PAGE_CACHE_UNAVAILABLE.set(unavailable)
    try:
        yield
    finally:
        _PAGE_CACHE_UNAVAILABLE.reset(unavailable_token)
        _ACTIVE_PAGE_CACHE.reset(cache_token)


def current_page_cache(url: str) -> PageCache | None:
    """Return the active cache; raise when the run's cache is unavailable."""
    if _PAGE_CACHE_UNAVAILABLE.get():
        raise PageCacheUnavailable(url)
    return _ACTIVE_PAGE_CACHE.get()


def replay_active() -> bool:
    cache = _ACTIVE_PAGE_CACHE.get()
    return (cache is not None and cache.mode is PageCacheMode.replay) or _PAGE_CACHE_UNAVAILABLE.get()


def earliest_recorded_at(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if not current:
        return candidate
    return min(current, candidate)
