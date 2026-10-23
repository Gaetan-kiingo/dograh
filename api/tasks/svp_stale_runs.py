"""P-33 (Swiss Voice Platform, ADR-002 / ADR-052; CALL-015): a voice run a crash left open
is finished, and the platform is told.

A process that dies (the loop watchdog's exit 70, a host restart) runs no handler: its
runs stay `running` or `initialized`, never completed, never notified, and the platform
would wait for them forever. Every five minutes this sweep completes any voice run older
than `SVP_STALE_RUN_AFTER_SECONDS` (two hours by default - far above any call's ceiling)
with the disposition `lost` and says `ended` for it. Text chats have their own sweep.
Runs only when `SVP_CALL_EVENTS_URL` is set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loguru import logger

from api.db import db_client
from api.enums import WorkflowRunState
from api.services import svp_call_events

LOST = "lost"


async def sweep_stale_voice_runs(_ctx) -> int:
    if not svp_call_events.enabled():
        return 0
    before = datetime.now(UTC) - timedelta(seconds=svp_call_events.STALE_RUN_AFTER_SECONDS)
    runs = await db_client.get_stale_incomplete_voice_runs(
        created_before=before, exclude_modes=svp_call_events.TEXT_MODES
    )
    finished = 0
    for run in runs:
        try:
            await db_client.update_workflow_run(
                run_id=run.id,
                is_completed=True,
                state=WorkflowRunState.COMPLETED.value,
                gathered_context={
                    **(run.gathered_context or {}),
                    "call_disposition": LOST,
                    "mapped_call_disposition": LOST,
                },
            )
        except Exception as e:  # noqa: BLE001 - the next pass tries again
            logger.error(f"P-33: stale run {run.id} not completed: {e}")
            continue
        await svp_call_events.ended_now(run.id, LOST, created_at=run.created_at.isoformat())
        finished += 1
    if finished:
        logger.warning(f"P-33: {finished} voice run(s) found open after a crash, finished as lost")
    return finished
