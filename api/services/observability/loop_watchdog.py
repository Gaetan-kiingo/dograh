"""A runtime whose event loop stops is ended, so that it is restarted (P-31, ADR-002).

One uvicorn worker carries every call of the process on one asyncio event loop. On
2026-10-05 a blocking call made on that loop never returned (Azure's
``Connection.open``, corrected by P-30): the process stayed up, answered no request,
started no call, and nothing restarted it - Docker marks a container unhealthy and
leaves it. Finding where it was stuck took a root shell on the host.

``SVP_LOOP_WATCHDOG_SECONDS=<n>`` starts a thread beside the loop. The loop renews a
mark every second; when the mark is older than ``n`` seconds - and still is a moment
later, so that a machine waking from sleep is not taken for a stopped loop - the thread
writes every thread's stack to stderr and ends the process with status 70. The
container's start script stops the container when a service exits, and the compose
file's restart policy starts it again.

Unset or 0: no watchdog (stock behaviour). The calls of a process whose loop has
stopped are already lost; ending it saves the next ones.
"""

import asyncio
import faulthandler
import os
import sys
import threading
import time
from collections.abc import Callable

from loguru import logger

ENV = "SVP_LOOP_WATCHDOG_SECONDS"
EXIT_STATUS = 70  # EX_SOFTWARE: an internal error, not a clean stop
_BEAT_SECONDS = 1.0

_watchdog: "LoopWatchdog | None" = None


def limit_from_env() -> float:
    """The limit in seconds, 0 when the watchdog is off or the value is not a number."""
    raw = os.getenv(ENV, "").strip()
    if not raw:
        return 0.0
    try:
        limit = float(raw)
    except ValueError:
        logger.warning(f"{ENV}={raw!r} is not a number of seconds: no loop watchdog")
        return 0.0
    return limit if limit > 0 else 0.0


def _end_process(stalled: float) -> None:
    """Say where every thread is, then end the process. Written straight to stderr:
    the log sinks may need the loop that has stopped."""
    sys.stderr.write(
        f"SVP loop watchdog: the event loop has not run for {stalled:.0f} s - "
        f"every thread's stack follows, then the process ends with status {EXIT_STATUS} "
        "(P-31)\n"
    )
    try:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    finally:
        sys.stderr.flush()
        os._exit(EXIT_STATUS)


class LoopWatchdog:
    """Watches one event loop from a thread of its own."""

    def __init__(
        self,
        limit: float,
        on_stall: Callable[[float], None] = _end_process,
        beat: float = _BEAT_SECONDS,
    ):
        self._limit = limit
        self._on_stall = on_stall
        self._beat_seconds = beat
        self._last = time.monotonic()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handle: asyncio.TimerHandle | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start on the running loop. Call from the loop's own thread."""
        self._loop = asyncio.get_running_loop()
        self._beat()
        self._thread = threading.Thread(
            target=self._watch, name="svp-loop-watchdog", daemon=True
        )
        self._thread.start()

    def _beat(self) -> None:
        # runs on the loop: as long as the loop runs, the mark is fresh
        self._last = time.monotonic()
        if not self._stop.is_set() and self._loop is not None:
            self._handle = self._loop.call_later(self._beat_seconds, self._beat)

    def _watch(self) -> None:
        while not self._stop.wait(self._beat_seconds):
            seen = self._last
            if time.monotonic() - seen <= self._limit:
                continue
            # Old - but a machine that slept wakes with an old mark and a loop that is
            # about to renew it. A stopped loop still has the same mark a moment later.
            if self._stop.wait(2 * self._beat_seconds):
                return
            if self._last != seen:
                continue
            self._on_stall(time.monotonic() - seen)
            return

    def stop(self) -> None:
        """Stop watching (a graceful shutdown may be slow and is not a stopped loop)."""
        self._stop.set()
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None


def start_from_env() -> LoopWatchdog | None:
    """Start the process's watchdog when the switch asks for one. Idempotent."""
    global _watchdog
    limit = limit_from_env()
    if not limit or _watchdog is not None:
        return _watchdog
    _watchdog = LoopWatchdog(limit)
    _watchdog.start()
    logger.info(
        f"Loop watchdog on: the process ends when its event loop stops for {limit:.0f} s"
    )
    return _watchdog


def stop() -> None:
    """Stop the process's watchdog. Safe to call when none was started."""
    global _watchdog
    watchdog, _watchdog = _watchdog, None
    if watchdog is not None:
        watchdog.stop()
