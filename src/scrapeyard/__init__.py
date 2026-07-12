"""Scrapeyard — Config-driven web scraping microservice."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("scrapeyard")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0+unknown"
