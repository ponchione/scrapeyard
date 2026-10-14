"""End a process that can no longer do its work, so its supervisor starts it again."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

logger = logging.getLogger(__name__)

# Exit status when the shutdown asked for did not finish in time (EX_SOFTWARE).
FORCED_EXIT_STATUS = 70

_lock = threading.Lock()
_requested = False


def end_process(reason: str, *, deadline_seconds: float) -> bool:
    """Shut the server down as a stop would, and end the process if that hangs.

    SIGTERM gives the server's own graceful shutdown, which runs the
    application's: running runs get the shutdown grace and record how they
    ended. A request that never finishes would hold that shutdown open, so a
    thread ends the process ``deadline_seconds`` later whatever its state.
    Returns False when an end was already requested.
    """

    global _requested
    with _lock:
        if _requested:
            return False
        _requested = True
    logger.critical(
        "Ending the process so that it is started again: %s; forcing the end "
        "in %.0fs if the shutdown has not finished",
        reason,
        deadline_seconds,
    )
    threading.Thread(
        target=_force_exit,
        args=(deadline_seconds,),
        name="scrapeyard-forced-exit",
        daemon=True,
    ).start()
    os.kill(os.getpid(), signal.SIGTERM)
    return True


def _force_exit(deadline_seconds: float) -> None:
    time.sleep(deadline_seconds)
    logger.critical(
        "Shutdown did not finish within %.0fs of the request to end the process; "
        "ending it now",
        deadline_seconds,
    )
    os._exit(FORCED_EXIT_STATUS)
