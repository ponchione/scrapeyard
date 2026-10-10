"""TD-71 diagnostic: how a renderer dies under the production container options.

Throwaway. Opens one page through BrowserSession exactly as the fetch-session
smoke check does, ends the renderer in the way TD71_KIND names, and reports
what Playwright, the driver and /proc saw afterwards as one JSON line.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from scrapling import PlayWrightFetcher
from scrapling.engines.pw import PlaywrightEngine

from scrapeyard.common.budgets import RunBudget
from scrapeyard.engine import browser_debug
from scrapeyard.engine.browser_session import BrowserSession

ORIGIN = "http://session.public.test:8080"
KIND = os.environ.get("TD71_KIND", "page_crash")
STEALTH = os.environ.get("TD71_STEALTH", "0") == "1"
CRASH_WAIT = float(os.environ.get("TD71_CRASH_WAIT", "20"))
LABEL = os.environ.get("TD71_LABEL", "local")
LOCAL = os.environ.get("TD71_LOCAL", "0") == "1"
OFFLINE_WAIT = float(os.environ.get("TD71_OFFLINE_WAIT", "15"))
# Candidate handling, applied to this process before any browser starts:
# rlimit0, rlimit1, filter0, or several joined with "+".
PYFIX = [name for name in os.environ.get("TD71_PYFIX", "").split("+") if name]
STARTED = time.monotonic()


def log(message: str) -> None:
    print(f"[probe {time.time():.3f} +{time.monotonic() - STARTED:6.2f}s] {message}", file=sys.stderr, flush=True)


class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        payload = b"<title>td71</title><h1>td71</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def read(path: str, limit: int = 8192) -> str:
    try:
        with open(path, "rb") as stream:
            return stream.read(limit).decode(errors="replace")
    except OSError as exc:
        return f"<{type(exc).__name__} errno={exc.errno}>"


def status_fields(pid: str) -> dict[str, str]:
    fields = {}
    for line in read(f"/proc/{pid}/status").splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            fields[name] = " ".join(value.split())
    return fields


def thread_states(pid: str) -> dict[str, int]:
    states: dict[str, int] = {}
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return states
    for tid in tids:
        stat = read(f"/proc/{pid}/task/{tid}/stat", 512)
        end = stat.rfind(")")
        state = stat[end + 2 : end + 3] if end != -1 else "?"
        states[state] = states.get(state, 0) + 1
    return states


def browser_processes(detail: bool) -> list[dict]:
    found = []
    for pid in sorted((name for name in os.listdir("/proc") if name.isdigit()), key=int):
        cmdline = read(f"/proc/{pid}/cmdline", 16384).replace("\0", " ").strip()
        if "chrom" not in cmdline and "crashpad" not in cmdline:
            continue
        if "td71_probe" in cmdline or "/driver/node" in cmdline:
            continue
        fields = status_fields(pid)
        kind = next((arg.split("=", 1)[1] for arg in cmdline.split() if arg.startswith("--type=")), "browser")
        entry = {
            "pid": int(pid),
            "type": kind,
            "state": fields.get("State", "?"),
            "threads": thread_states(pid),
            "coredumping": fields.get("CoreDumping"),
        }
        if detail:
            try:
                owner = os.stat(f"/proc/{pid}").st_uid
            except OSError as exc:
                owner = f"errno={exc.errno}"
            entry.update(
                ppid=fields.get("PPid"),
                nspid=fields.get("NSpid"),
                uid=fields.get("Uid"),
                proc_dir_owner=owner,
                sigcgt=fields.get("SigCgt"),
                sigign=fields.get("SigIgn"),
                sigblk=fields.get("SigBlk"),
                capeff=fields.get("CapEff"),
                capbnd=fields.get("CapBnd"),
                nonewprivs=fields.get("NoNewPrivs"),
                seccomp=fields.get("Seccomp"),
                seccomp_filters=fields.get("Seccomp_filters"),
                rss=fields.get("VmRSS"),
                vmsize=fields.get("VmSize"),
                core_limit=next(
                    (" ".join(line.split()[4:6]) for line in read(f"/proc/{pid}/limits").splitlines()
                     if line.startswith("Max core file size")), None),
                cwd=_readlink(f"/proc/{pid}/cwd"),
                root=_readlink(f"/proc/{pid}/root"),
                oom_score_adj=read(f"/proc/{pid}/oom_score_adj", 16).strip(),
                coredump_filter=read(f"/proc/{pid}/coredump_filter", 16).strip(),
            )
            if kind == "browser":
                entry["cmdline"] = cmdline
        found.append(entry)
    return found


def _readlink(path: str) -> str:
    try:
        return os.readlink(path)
    except OSError as exc:
        return f"<errno={exc.errno}>"


def handles(sigcgt: str | None, signum: int) -> bool | None:
    if not sigcgt:
        return None
    return bool(int(sigcgt, 16) >> (signum - 1) & 1)


JS_HEAP_OOM = """() => { const keep = []; for (;;) { keep.push(new Array(4096).fill(0).map((_, i) => ({i, s: 'x' + i}))); } }"""
JS_BUFFER_OOM = """async () => { const keep = []; for (;;) { keep.push(new Uint8Array(64 * 1024 * 1024).fill(1)); await new Promise(r => setTimeout(r, 0)); } }"""


async def probe() -> dict:
    result: dict = {"label": LABEL, "kind": KIND, "stealth": STEALTH, "crash_wait": CRASH_WAIT}
    session = BrowserSession(PlayWrightFetcher)
    guard = browser_debug._guarded_async_intercept_route
    budget = RunBudget(
        max_duration_seconds=600, max_fetched_bytes=1_000_000,
        max_extracted_records=100, max_serialized_result_bytes=100_000,
        max_browser_debug_bytes=100_000,
    )

    async def action(page):
        browser = page.context.browser
        events: dict[str, float] = {}
        sent = time.monotonic()

        def mark(name):
            events.setdefault(name, round(time.monotonic() - sent, 3))
            log(f"event {name} at +{events[name]}s after the kill was sent")

        crashed = asyncio.Event()
        page.on("crash", lambda _: (mark("page_crash"), crashed.set()))
        page.on("close", lambda _: mark("page_close"))
        browser.on("disconnected", lambda _: mark("browser_disconnected"))

        before = browser_processes(detail=True)
        result["before"] = before
        renderers = [entry["pid"] for entry in before if entry["type"] == "renderer"]
        result["handlers"] = {
            str(entry["pid"]): {
                name: handles(entry.get("sigcgt"), number)
                for name, number in (("SIGTRAP", 5), ("SIGILL", 4), ("SIGSEGV", 11), ("SIGABRT", 6), ("SIGTERM", 15))
            }
            for entry in before if entry["type"] in ("renderer", "zygote")
        }
        log(f"renderers before: {renderers}")

        pending = None
        sent = time.monotonic()
        log(f"sending kind={KIND}")
        if KIND == "page_crash":
            cdp = await page.context.new_cdp_session(page)
            sent = time.monotonic()
            pending = asyncio.create_task(cdp.send("Page.crash"))
        elif KIND in ("sigkill", "sigsegv", "sigtrap", "sigabrt", "sigterm"):
            number = {"sigkill": signal.SIGKILL, "sigsegv": signal.SIGSEGV, "sigtrap": signal.SIGTRAP,
                      "sigabrt": signal.SIGABRT, "sigterm": signal.SIGTERM}[KIND]
            sent_to = {}
            for pid in renderers:
                try:
                    os.kill(pid, number)
                    sent_to[pid] = "sent"
                except OSError as exc:
                    sent_to[pid] = f"errno={exc.errno}"
            result["signal_sent"] = sent_to
        elif KIND in ("checkcrash", "nullcrash", "memory_exhaust", "kill_url"):
            url = {"checkcrash": "chrome://checkcrash", "nullcrash": "chrome://crash",
                   "memory_exhaust": "chrome://memory-exhaust", "kill_url": "chrome://kill"}[KIND]
            pending = asyncio.create_task(page.goto(url))
        elif KIND == "js_heap_oom":
            pending = asyncio.create_task(page.evaluate(JS_HEAP_OOM))
        elif KIND == "js_buffer_oom":
            pending = asyncio.create_task(page.evaluate(JS_BUFFER_OOM))
        elif KIND == "none":
            pass
        else:
            raise SystemExit(f"unknown kind {KIND}")

        samples = []
        while time.monotonic() - sent < CRASH_WAIT and not crashed.is_set():
            try:
                await asyncio.wait_for(crashed.wait(), 1)
            except asyncio.TimeoutError:
                pass
            samples.append({"t": round(time.monotonic() - sent, 2), "procs": browser_processes(detail=False)})
        result["samples"] = samples[:3] + samples[-2:] if len(samples) > 5 else samples
        result["crash_event_seconds"] = events.get("page_crash")
        if pending is not None:
            if pending.done():
                result["command"] = repr(pending.exception() or pending.result())[:300]
            else:
                result["command"] = "pending"
        log(f"crash event: {events.get('page_crash')}")

        started = time.monotonic()
        try:
            await asyncio.wait_for(page.context.set_offline(True), OFFLINE_WAIT)
            result["set_offline"] = "answered"
        except asyncio.TimeoutError:
            result["set_offline"] = f"no answer in {OFFLINE_WAIT:g}s"
        except Exception as exc:  # noqa: BLE001
            result["set_offline"] = f"error: {type(exc).__name__}: {str(exc)[:160]}"
        result["set_offline_seconds"] = round(time.monotonic() - started, 3)
        log(f"set_offline: {result['set_offline']} after {result['set_offline_seconds']}s")
        result["connected_after"] = browser.is_connected()
        result["after"] = browser_processes(detail=False)
        result["events"] = dict(events)
        if pending is not None and not pending.done():
            pending.cancel()
        result["_events_ref"] = events
        raise RuntimeError("td71 probe finished")

    started = time.monotonic()
    try:
        await session.fetch(
            ORIGIN + "/failure",
            {"headless": True, "page_action": action, "stealth": STEALTH},
            guard, budget=budget,
        )
    except (RuntimeError, httpx.NetworkError) as exc:
        result["fetch_raised"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    except BaseException as exc:  # noqa: BLE001
        result["fetch_raised"] = f"UNEXPECTED {type(exc).__name__}: {str(exc)[:300]}"
    result["fetch_seconds"] = round(time.monotonic() - started, 3)
    started = time.monotonic()
    await session.aclose()
    result["aclose_seconds"] = round(time.monotonic() - started, 3)
    events = result.pop("_events_ref", {})
    result["events"] = dict(events)
    result["left_over"] = browser_processes(detail=False)
    return result


def apply_pyfix() -> dict:
    import resource

    applied = {}
    for name in PYFIX:
        try:
            if name == "rlimit0":
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            elif name == "rlimit1":
                resource.setrlimit(resource.RLIMIT_CORE, (1, 1))
            elif name == "filter0":
                with open("/proc/self/coredump_filter", "w") as stream:
                    stream.write("0")
            else:
                raise SystemExit(f"unknown pyfix {name}")
            applied[name] = "ok"
        except OSError as exc:
            applied[name] = f"errno={exc.errno} {exc.strerror}"
    return applied


def environment() -> dict:
    import resource

    return {
        "pyfix": apply_pyfix(),
        "core_pattern": read("/proc/sys/kernel/core_pattern").strip(),
        "core_pipe_limit": read("/proc/sys/kernel/core_pipe_limit").strip(),
        "rlimit_core": list(resource.getrlimit(resource.RLIMIT_CORE)),
        "coredump_filter": read("/proc/self/coredump_filter").strip(),
        "cwd": os.getcwd(),
        "cwd_writable": os.access(os.getcwd(), os.W_OK),
        "uname": " ".join(os.uname()),
    }


async def main() -> None:
    env = environment()
    log(f"environment: {env}")
    if LOCAL:
        # No container network here: answer a public address to the URL guard
        # and send the browser to the loopback fixture.
        real = socket.getaddrinfo

        def mapped(host, *args, **kwargs):
            if isinstance(host, str) and host.endswith(".public.test"):
                return real("203.66.81.243", *args, **kwargs)
            return real(host, *args, **kwargs)

        socket.getaddrinfo = mapped
        launch_kwargs = PlaywrightEngine._PlaywrightEngine__launch_kwargs

        def mapped_launch_kwargs(engine):
            kwargs = launch_kwargs(engine)
            return {**kwargs, "args": [*kwargs.get("args", []), "--host-resolver-rules=MAP *.public.test 127.0.0.1"]}

        PlaywrightEngine._PlaywrightEngine__launch_kwargs = mapped_launch_kwargs
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    result: dict = {"label": LABEL, "kind": KIND, "stealth": STEALTH}
    try:
        result = await asyncio.wait_for(probe(), CRASH_WAIT + OFFLINE_WAIT + 120)
    except asyncio.TimeoutError:
        result["probe"] = "timed out"
    except BaseException as exc:  # noqa: BLE001
        result["probe"] = f"failed: {type(exc).__name__}: {str(exc)[:400]}"
    result["environment"] = env
    print(json.dumps(result), flush=True)
    sys.stdout.flush()
    # A browser that never answers would hold a normal exit.
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
