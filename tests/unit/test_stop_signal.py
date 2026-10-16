"""The stop-signal hook wraps only Python handlers and acts once per stop."""

from __future__ import annotations

import signal
from unittest.mock import MagicMock

from scrapeyard.runtime.stop_signal import StopSignalHook


def test_hook_acts_once_and_leaves_a_default_handler_alone(request) -> None:
    original_usr1 = signal.getsignal(signal.SIGUSR1)
    original_usr2 = signal.signal(signal.SIGUSR2, signal.SIG_DFL)
    request.addfinalizer(lambda: signal.signal(signal.SIGUSR1, original_usr1))
    request.addfinalizer(lambda: signal.signal(signal.SIGUSR2, original_usr2))
    server_stops: list[int] = []

    def server_handler(signum, _frame) -> None:
        server_stops.append(signum)

    signal.signal(signal.SIGUSR1, server_handler)
    on_stop = MagicMock()
    hook = StopSignalHook(on_stop, signals=(signal.SIGUSR1, signal.SIGUSR2))

    assert hook.install()
    # Wrapping the default would turn a signal that ends the process into one
    # that does nothing.
    assert signal.getsignal(signal.SIGUSR2) == signal.SIG_DFL

    signal.raise_signal(signal.SIGUSR1)
    signal.raise_signal(signal.SIGUSR1)
    on_stop.assert_called_once_with()
    assert hook.requested_at is not None
    assert server_stops == [signal.SIGUSR1, signal.SIGUSR1]

    hook.restore()
    assert signal.getsignal(signal.SIGUSR1) is server_handler
