"""Deterministic HTTP fixture used only by the real container/browser smoke lane."""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


PUBLIC_ORIGIN = "http://fixture.public.test:8080"
PRIVATE_ORIGIN = "http://fixture.private.test:8080"


class FixtureState:
    """Thread-safe protected-endpoint observations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._protected = Counter()
        self._requests = Counter()
        self._webhook_attempts = Counter()
        self._webhook_delivered = Counter()

    def record_protected(self, case: str) -> None:
        with self._lock:
            self._protected[case] += 1

    def record_request(self, path: str) -> None:
        with self._lock:
            self._requests[path] += 1

    def record_webhook_attempt(self, case: str) -> int:
        with self._lock:
            self._webhook_attempts[case] += 1
            return self._webhook_attempts[case]

    def record_webhook_delivery(self, case: str) -> None:
        with self._lock:
            self._webhook_delivered[case] += 1

    def reset(self) -> None:
        with self._lock:
            self._protected.clear()
            self._requests.clear()
            self._webhook_attempts.clear()
            self._webhook_delivered.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cases = dict(sorted(self._protected.items()))
            requests = dict(sorted(self._requests.items()))
            webhook_attempts = dict(sorted(self._webhook_attempts.items()))
            webhook_delivered = dict(sorted(self._webhook_delivered.items()))
        return {
            "protected_total": sum(cases.values()),
            "protected_cases": cases,
            "requests": requests,
            "webhook_attempts": webhook_attempts,
            "webhook_delivered": webhook_delivered,
        }


STATE = FixtureState()


STATIC_HTML = b"""<!doctype html>
<html><head><title>Scrapeyard static fixture</title></head>
<body>
  <main id="static-fixture">
    <h1 id="static-value">static-ok</h1>
    <p id="redirect-marker">safe-redirect-ok</p>
  </main>
</body></html>
"""


DYNAMIC_HTML = b"""<!doctype html>
<html><head><title>Scrapeyard browser fixture</title>
<style>
  body { margin: 0; font-family: sans-serif; }
  #consent { position: fixed; inset: 0; z-index: 5; background: white; padding: 2rem; }
  #spacer { height: 2400px; background: linear-gradient(white, #ddd); }
</style></head>
<body>
  <div id="consent"><p>Fixture consent interaction</p><button id="accept-consent">Accept</button></div>
  <main>
    <h1 id="fixture-heading">browser-fixture</h1>
    <div id="js-root"></div>
    <div id="consent-result"></div>
    <div id="spacer"></div>
    <div id="scroll-result"></div>
    <button id="load-more">Load more</button>
    <span id="loaded-count">0</span>
    <div id="loaded-items"></div>
  </main>
<script>
document.addEventListener('DOMContentLoaded', () => {
  const js = document.createElement('p');
  js.id = 'js-value';
  js.textContent = 'javascript-ok';
  document.querySelector('#js-root').appendChild(js);
});
document.querySelector('#accept-consent').addEventListener('click', () => {
  document.querySelector('#consent').remove();
  const value = document.createElement('p');
  value.id = 'consent-value';
  value.textContent = 'consent-ok';
  document.querySelector('#consent-result').appendChild(value);
});
window.addEventListener('scroll', () => {
  if (window.scrollY > 900 && !document.querySelector('#scroll-value')) {
    const value = document.createElement('p');
    value.id = 'scroll-value';
    value.textContent = 'scroll-ok';
    document.querySelector('#scroll-result').appendChild(value);
  }
});
let loaded = 0;
document.querySelector('#load-more').addEventListener('click', () => {
  loaded += 1;
  document.querySelector('#loaded-count').textContent = String(loaded);
  const value = document.createElement('p');
  value.className = 'loaded-item';
  value.textContent = `loaded-${loaded}`;
  document.querySelector('#loaded-items').appendChild(value);
  if (loaded === 2) {
    const ready = document.createElement('span');
    ready.id = 'loaded-ready';
    ready.textContent = 'load-more-ok';
    document.querySelector('#loaded-items').appendChild(ready);
  }
});
</script></body></html>
"""


UNSAFE_SUBREQUEST_HTML = f"""<!doctype html>
<html><head><title>Scrapeyard unsafe subrequest fixture</title></head>
<body>
  <p id="safe-value">safe-page-ok</p>
  <button id="trigger-unsafe">Trigger private request</button>
  <div id="subrequest-state">not-started</div>
<script>
document.querySelector('#trigger-unsafe').addEventListener('click', async () => {{
  const state = document.querySelector('#subrequest-state');
  state.textContent = 'started';
  try {{
    await fetch('{PRIVATE_ORIGIN}/protected?case=browser-subrequest', {{mode: 'no-cors'}});
    state.textContent = 'unexpectedly-reached';
  }} catch (_error) {{
    state.textContent = 'blocked';
  }}
  state.id = 'subrequest-finished';
}});
</script></body></html>
""".encode()


class FixtureRequestHandler(BaseHTTPRequestHandler):
    server_version = "ScrapeyardSmokeFixture/1"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        parsed = urlsplit(self.path)
        STATE.record_request(parsed.path)
        if parsed.path == "/health":
            self._json({"status": "ok"})
            return
        if parsed.path == "/static":
            self._html(STATIC_HTML)
            return
        if parsed.path == "/dynamic":
            self._html(DYNAMIC_HTML)
            return
        if parsed.path == "/delay":
            seconds = min(60.0, max(0.0, _float_query(parsed.query, "seconds", 0.0)))
            time.sleep(seconds)
            case = parse_qs(parsed.query).get("case", ["delay-ok"])[0]
            self._html(
                f"<!doctype html><div class='record'><span class='value'>{case}</span></div>".encode()
            )
            return
        if parsed.path == "/large":
            count = min(100_001, max(1, _int_query(parsed.query, "count", 1)))
            value_size = min(65_536, max(1, _int_query(parsed.query, "value_size", 16)))
            value = "x" * value_size
            records = "".join(
                f"<div class='record'><span class='index'>{index}</span>"
                f"<span class='value'>{value}</span></div>"
                for index in range(count)
            )
            self._html(f"<!doctype html><main>{records}</main>".encode())
            return
        if parsed.path == "/unsafe-subrequest":
            self._html(UNSAFE_SUBREQUEST_HTML)
            return
        if parsed.path == "/redirect-safe":
            self._redirect(f"{PUBLIC_ORIGIN}/static")
            return
        if parsed.path == "/redirect-unsafe":
            self._redirect(f"{PRIVATE_ORIGIN}/protected?case=redirect")
            return
        if parsed.path == "/protected":
            case = parse_qs(parsed.query).get("case", ["unspecified"])[0]
            STATE.record_protected(case)
            self._json({"error": "protected endpoint was reached", "case": case})
            return
        if parsed.path == "/__stats":
            self._json(STATE.snapshot())
            return
        if parsed.path == "/__reset":
            STATE.reset()
            self._json(STATE.snapshot())
            return
        self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        parsed = urlsplit(self.path)
        STATE.record_request(parsed.path)
        if parsed.path != "/webhook":
            self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        length = _int_header(self.headers.get("Content-Length"))
        payload = self.rfile.read(length) if length else b""
        try:
            decoded = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, status=HTTPStatus.BAD_REQUEST)
            return
        case = parse_qs(parsed.query).get("case", ["default"])[0]
        failures = max(0, _int_query(parsed.query, "failures", 0))
        attempt = STATE.record_webhook_attempt(case)
        if attempt <= failures:
            self._json(
                {"case": case, "attempt": attempt, "outcome": "retry"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        STATE.record_webhook_delivery(case)
        self._json(
            {
                "case": case,
                "attempt": attempt,
                "outcome": "delivered",
                "event": decoded.get("event"),
            }
        )

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep fixture diagnostics compact while retaining every requested path.
        print(f"fixture request: {self.address_string()} {fmt % args}", flush=True)

    def _send(self, payload: bytes, content_type: str, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, payload: bytes) -> None:
        self._send(payload, "text/html; charset=utf-8", HTTPStatus.OK)

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, sort_keys=True).encode()
        self._send(payload, "application/json", status)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), FixtureRequestHandler)


def _int_query(query: str, name: str, default: int) -> int:
    try:
        return int(parse_qs(query).get(name, [str(default)])[0])
    except ValueError:
        return default


def _float_query(query: str, name: str, default: float) -> float:
    try:
        return float(parse_qs(query).get(name, [str(default)])[0])
    except ValueError:
        return default


def _int_header(value: str | None) -> int:
    try:
        return max(0, int(value or "0"))
    except ValueError:
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    print(f"fixture listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
