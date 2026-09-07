"""Measure target pagination against a controlled, keep-alive HTTP fixture.

Run in a fresh browser-capable container on an isolated public-address network;
--host must resolve to that container. No destination guards are patched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scrapeyard.config.schema import RetryConfig, TargetConfig
from scrapeyard.engine.scraper import scrape_target


class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    connections = 0
    requests = 0

    def setup(self):
        super().setup()
        type(self).connections += 1

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        type(self).requests += 1
        query = parse_qs(urlsplit(self.path).query)
        page = int(query.get("page", [1])[0])
        pages = int(query.get("pages", [5])[0])
        body = f"<html><title>Page {page}</title><h1>page-{page}</h1>"
        if page < pages:
            body += f'<a rel="next" href="?page={page + 1}&pages={pages}">Next</a>'
        payload = (body + "</html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


async def benchmark(args):
    launches = 0

    def count_launch(original):
        async def launch(self, *positional, **kwargs):
            nonlocal launches
            launches += 1
            return await original(self, *positional, **kwargs)
        return launch

    server = ThreadingHTTPServer(("0.0.0.0", 8080), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ExitStack() as stack, TemporaryDirectory() as data_dir:
            from playwright.async_api import BrowserType
            from rebrowser_playwright.async_api import BrowserType as RebrowserType

            for browser_type in (BrowserType, RebrowserType):
                stack.enter_context(patch.object(
                    browser_type, "launch", count_launch(browser_type.launch),
                ))
            target = TargetConfig.model_validate({
                "url": f"http://{args.host}:8080/?pages={args.pages}",
                "fetcher": args.fetcher,
                "selectors": {"title": "h1"},
                "pagination": {"next": "a[rel=next]", "max_pages": args.pages},
            })
            started = time.monotonic()
            result = await scrape_target(
                target, adaptive=False, retry=RetryConfig(max_attempts=1),
                adaptive_dir=data_dir,
            )
            elapsed = time.monotonic() - started
            assert result.status == "success", result
            assert result.pages_scraped == args.pages, result
            assert result.pagination_stop_reason == "exhausted", result
            assert [row["title"] for row in result.data] == [
                f"page-{n}" for n in range(1, args.pages + 1)
            ], result.data
            print(json.dumps({
                "fetcher": args.fetcher, "pages": args.pages,
                "elapsed_seconds": round(elapsed, 3),
                "physical_connections": Fixture.connections,
                "requests": Fixture.requests, "browser_launches": launches,
                "peak_container_bytes": int(Path("/sys/fs/cgroup/memory.peak").read_text()),
            }), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="fixture.public.test")
    parser.add_argument("--fetcher", choices=("basic", "dynamic", "stealthy"), required=True)
    parser.add_argument("--pages", type=int, default=5)
    asyncio.run(benchmark(parser.parse_args()))
