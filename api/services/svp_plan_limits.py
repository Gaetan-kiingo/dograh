"""P-55 (Swiss Voice Platform, ADR-002; E2b, ADR-083 amendment; BIL-006, BIL-007): a plan's
simultaneous calls.

``SVP_PLAN_LIMITS=on`` (on / 1 / true):

1. **The platform sets an organisation's limit** through
   ``PUT /api/v1/svp/organizations/{organization_id}/concurrent-call-limit``
   ``{"value": 1..1000 | null}`` with ``Authorization: Bearer <SVP_KEY_SERVICE_TOKEN>`` (the
   platform's credential to the runtime, P-17; compared in constant time, refused when the
   runtime has none). The value is written as the organisation's stock
   ``CONCURRENT_CALL_LIMIT`` configuration, which ``call_concurrency`` reads from the database
   at every slot - no cache, so the next call obeys it on every worker; ``null`` deletes it
   (the runtime's default, ``DEFAULT_ORG_CONCURRENCY_LIMIT``).
2. **A phone call over the limit is refused as busy**: the ARI path hangs the unanswered
   channel up with cause 17 (user busy) - the caller hears the carrier's busy tone -, Twilio's
   path answers ``<Reject reason="busy"/>``.
3. **The member's own browser test takes no slot** (the owner, 2026-11-11): on the public
   embed door, a run whose ``initial_context.source`` is the platform's ``svp-voice-test`` is
   not counted; the website widget (``svp-widget``) is. The platform sets the source when it
   brokers the embed session (F2d) - the runtime's public init is not on the front door.

Unset = stock behaviour: the door answers 404, the stock refusals, every browser call counted.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from loguru import logger

from api.db import db_client
from api.enums import OrganizationConfigurationKey

LIMIT_MIN = 1
LIMIT_MAX = 1000
BUSY_CAUSE = 17  # Q.850 user busy
BUSY_TWIML = (
    '<?xml version="1.0" encoding="UTF-8"?><Response><Reject reason="busy"/></Response>'
)
UNCOUNTED_SOURCE = "svp-voice-test"


def enabled() -> bool:
    return os.getenv("SVP_PLAN_LIMITS", "").strip().lower() in ("on", "1", "true")


def platform_bearer_ok(authorization: str | None) -> bool:
    """The platform's ``Bearer <SVP_KEY_SERVICE_TOKEN>``, compared in constant time; False
    when the runtime has no token (fail closed)."""
    token = os.getenv("SVP_KEY_SERVICE_TOKEN", "").strip()
    scheme, _, credential = str(authorization or "").strip().partition(" ")
    if not token or scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(credential.strip().encode(), token.encode())


async def set_concurrent_call_limit(organization_id: int, value: int | None) -> bool:
    """Write (or, with None, delete) the organisation's ``CONCURRENT_CALL_LIMIT``; False when
    the organisation does not exist."""
    if await db_client.get_organization_by_id(organization_id) is None:
        return False
    key = OrganizationConfigurationKey.CONCURRENT_CALL_LIMIT.value
    if value is None:
        await db_client.delete_configuration(organization_id, key)
    else:
        await db_client.upsert_configuration(
            organization_id, key, {"value": int(value)}
        )
    logger.info(
        f"P-55: concurrent-call limit of org {organization_id} set to "
        f"{value if value is not None else 'the default'}"
    )
    return True


def takes_slot(workflow_run: Any) -> bool:
    """Whether a browser call on the public embed door takes a slot: always, unless the
    switch is on and the run is the member's own test (``svp-voice-test``)."""
    if not enabled():
        return True
    context = getattr(workflow_run, "initial_context", None)
    if isinstance(context, dict) and context.get("source") == UNCOUNTED_SOURCE:
        logger.info(
            f"P-55: run {getattr(workflow_run, 'id', '?')} is the member's test - no call slot"
        )
        return False
    return True


def twilio_busy_response():
    """Twilio's verb for a call over the plan's limit: the caller hears busy."""
    from fastapi.responses import Response

    return Response(content=BUSY_TWIML, media_type="application/xml")
