"""Tests for ending a process that can no longer do its work."""

from __future__ import annotations

import os
import signal

import pytest

from scrapeyard.runtime import process_exit


@pytest.fixture()
def harness(monkeypatch):
    calls: list[tuple] = []
    threads: list[tuple] = []

    class _Thread:
        def __init__(self, *, target, args, name, daemon):
            threads.append((target, args, name, daemon))

        def start(self):
            calls.append(("thread_started",))

    monkeypatch.setattr(process_exit, "_requested", False)
    monkeypatch.setattr(process_exit.threading, "Thread", _Thread)
    monkeypatch.setattr(process_exit.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(process_exit.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(process_exit.os, "_exit", lambda status: calls.append(("exit", status)))
    return calls, threads


def test_end_process_asks_for_a_graceful_shutdown_once_and_forces_it_later(harness):
    calls, threads = harness

    assert process_exit.end_process("worker ended", deadline_seconds=40) is True
    assert process_exit.end_process("worker ended again", deadline_seconds=40) is False

    # The forced end is armed before the signal, so a shutdown that hangs at
    # once is still ended.
    assert calls == [("thread_started",), ("kill", os.getpid(), signal.SIGTERM)]
    [(target, args, _name, daemon)] = threads
    assert daemon is True

    target(*args)

    assert calls[-2:] == [("sleep", 40), ("exit", process_exit.FORCED_EXIT_STATUS)]
