"""Act on a stop when its signal arrives, before the server drains its requests."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Iterable
from types import FrameType
from typing import Any

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class StopSignalHook:
    """Call ``on_stop`` once when a stop signal arrives, then the handler before it.

    Uvicorn answers a stop by waiting for the requests in flight and only then
    running the application's shutdown, which is what stops the worker. Until
    then the worker went on starting runs, and the shutdown grace, which
    Compose's stop period is sized for, started late. The hook lets both start
    at the signal.

    It only wraps a handler set from Python, such as uvicorn's, and leaves a
    signal whose handler is the default or ignored alone. ``on_stop`` runs
    inside the signal handler, so it must only set state and must not raise.
    """

    def __init__(
        self,
        on_stop: Callable[[], None],
        *,
        signals: Iterable[signal.Signals] = STOP_SIGNALS,
    ) -> None:
        self._on_stop = on_stop
        self._signals = tuple(signals)
        self._previous: dict[signal.Signals, Callable[[int, FrameType | None], Any]] = {}
        self.requested_at: float | None = None

    def install(self) -> bool:
        """Wrap each stop signal's Python handler; return whether any was wrapped."""
        if threading.current_thread() is not threading.main_thread():
            return False
        for signum in self._signals:
            previous = signal.getsignal(signum)
            if callable(previous):
                self._previous[signum] = previous
                signal.signal(signum, self._handle)
        return bool(self._previous)

    def restore(self) -> None:
        """Put back each handler that was wrapped, unless it was replaced since."""
        for signum, handler in self._previous.items():
            if signal.getsignal(signum) == self._handle:
                signal.signal(signum, handler)
        self._previous = {}

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        try:
            if self.requested_at is None:
                self.requested_at = time.monotonic()
                self._on_stop()
        finally:
            previous = self._previous.get(signal.Signals(signum))
            if previous is not None:
                previous(signum, frame)
