"""P-31 (ADR-002): a runtime whose event loop stops is ended, so that it is restarted.

On 2026-10-05 a blocking call on the loop never returned; the process answered nothing
and nothing restarted it. The watchdog is a thread beside the loop.
"""

import asyncio
import subprocess
import sys
import textwrap
import time

import pytest

from api.services.observability import loop_watchdog
from api.services.observability.loop_watchdog import LoopWatchdog


def test_the_switch_is_a_number_of_seconds_and_unset_means_no_watchdog(monkeypatch):
    monkeypatch.delenv(loop_watchdog.ENV, raising=False)
    assert loop_watchdog.limit_from_env() == 0.0
    for raw, expected in (
        ("20", 20.0),
        (" 7.5 ", 7.5),
        ("0", 0.0),
        ("-3", 0.0),
        ("on", 0.0),
    ):
        monkeypatch.setenv(loop_watchdog.ENV, raw)
        assert loop_watchdog.limit_from_env() == expected


@pytest.mark.asyncio
async def test_unset_nothing_is_started(monkeypatch):
    monkeypatch.delenv(loop_watchdog.ENV, raising=False)
    assert loop_watchdog.start_from_env() is None
    loop_watchdog.stop()  # safe when none was started


@pytest.mark.asyncio
async def test_a_loop_that_stops_is_noticed():
    stalls: list[float] = []
    watchdog = LoopWatchdog(0.3, on_stall=stalls.append, beat=0.05)
    watchdog.start()
    try:
        await asyncio.sleep(0.5)  # a loop that runs: nothing, however long
        assert stalls == []
        time.sleep(1.0)  # noqa: ASYNC251 - a blocking call on the loop, as on 2026-10-05
        assert len(stalls) == 1 and stalls[0] > 0.3
    finally:
        watchdog.stop()


@pytest.mark.asyncio
async def test_a_pause_shorter_than_the_limit_is_not_a_stopped_loop():
    stalls: list[float] = []
    watchdog = LoopWatchdog(1.0, on_stall=stalls.append, beat=0.05)
    watchdog.start()
    try:
        time.sleep(0.4)  # noqa: ASYNC251 - the pause is the test
        await asyncio.sleep(0.3)
        assert stalls == []
    finally:
        watchdog.stop()


@pytest.mark.asyncio
async def test_an_old_mark_that_is_renewed_at_once_is_a_machine_that_slept():
    # after a suspend the mark is old and the loop is about to renew it: the watchdog
    # looks again a moment later before it ends anything
    stalls: list[float] = []
    watchdog = LoopWatchdog(0.3, on_stall=stalls.append, beat=0.05)
    watchdog.start()
    try:
        watchdog._last -= 60  # as if a minute had passed without the loop running
        await asyncio.sleep(0.5)  # the loop runs: the mark is renewed
        assert stalls == []
    finally:
        watchdog.stop()


@pytest.mark.asyncio
async def test_stopped_it_ends_nothing():
    stalls: list[float] = []
    watchdog = LoopWatchdog(0.2, on_stall=stalls.append, beat=0.05)
    watchdog.start()
    watchdog.stop()  # the graceful shutdown
    time.sleep(0.6)  # noqa: ASYNC251 - the loop is blocked on purpose
    await asyncio.sleep(0.1)
    assert stalls == []


def test_the_process_ends_with_status_70_and_leaves_every_threads_stack():
    script = textwrap.dedent(
        """
        import asyncio, time
        from api.services.observability.loop_watchdog import LoopWatchdog

        def blocking_call_that_never_returns():
            time.sleep(30)

        async def main():
            LoopWatchdog(0.3, beat=0.05).start()
            await asyncio.sleep(0.1)
            blocking_call_that_never_returns()

        asyncio.run(main())
        """
    )
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert done.returncode == loop_watchdog.EXIT_STATUS
    assert time.monotonic() - started < 10  # not the 30 s of the blocked call
    assert "the event loop has not run for" in done.stderr
    # the evidence: where the loop's thread was
    assert "blocking_call_that_never_returns" in done.stderr
