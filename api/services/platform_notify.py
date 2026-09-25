"""P-20 (Swiss Voice Platform, ADR-002 / ADR-037 point 8): the platform told when a
call ended.

With ``SVP_CALL_ENDED_URL`` set, the workflow-completion task - after the artifacts are
uploaded and the post-call integrations ran - posts ``{organization_id, workflow_id,
run_id}`` to it with the bearer ``SVP_KEY_SERVICE_TOKEN`` (the runtime's credential to
the platform, P-17). The platform syncs that workspace's calls at once, so the
business's webhook (``call.completed``) leaves within seconds instead of at the next
five-minute pass. A failure is logged and never raised: the platform's own sync still
ingests the run. Unset = stock behaviour (no request).
"""

from __future__ import annotations

import os

import httpx
from loguru import logger

CALL_ENDED_TIMEOUT = 5.0


def call_ended_url() -> str:
    return os.getenv("SVP_CALL_ENDED_URL", "").strip()


async def notify_call_ended(workflow_run_id: int) -> bool:
    """True when the platform acknowledged the notice."""
    url = call_ended_url()
    if not url:
        return False
    token = os.getenv("SVP_KEY_SERVICE_TOKEN", "").strip()
    if not token:
        logger.warning(
            "SVP_CALL_ENDED_URL is set but SVP_KEY_SERVICE_TOKEN is not: the platform is not told"
        )
        return False
    try:
        from api.db import db_client

        workflow_run, organization_id = await db_client.get_workflow_run_with_context(
            workflow_run_id
        )
    except Exception as e:  # noqa: BLE001 - a notice never breaks the completion
        logger.warning(f"call-ended notice: run {workflow_run_id} not read ({type(e).__name__})")
        return False
    if not workflow_run or not organization_id:
        return False
    body = {
        "organization_id": organization_id,
        "workflow_id": workflow_run.workflow_id,
        "run_id": workflow_run_id,
    }
    try:
        async with httpx.AsyncClient(timeout=CALL_ENDED_TIMEOUT) as client:
            response = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as e:
        logger.warning(
            f"call-ended notice not delivered ({type(e).__name__}); the platform's sync "
            "will ingest the run"
        )
        return False
    if response.status_code >= 300:
        logger.warning(f"call-ended notice refused by the platform (HTTP {response.status_code})")
        return False
    return True
