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

**Amended 2026-11-12 (E2c, ADR-083 amendment): the caller over the limit leaves a message.**
With ``SVP_BUSY_VOICEMAIL=on`` too, before refusing the runtime tells the platform
(``SVP_BUSY_REPORT_URL``, else ``SVP_CALLER_SCREEN_URL`` with ``/calls/screen`` read as
``/calls/busy``; the same bearer)::

    POST {"organization_id", "provider": "ari" | "twilio", "called_number", "carrier_call_id"}
    200  {"action": "voicemail" | "busy", "language": "fr" | "de" | "it" | "en" | null}

The platform counts the caller (no caller data) and, on ``voicemail``, the ARI path sets the
channel's ``SVP_BUSY_LANG`` and hands the unanswered channel back to the edge's dialplan at
``svp-busy,s,1`` - the busy greeting in that language, the tone, the recording; Twilio's path
answers 503 and Twilio calls the number's fallback URL, the platform's busy greeting and
record. **Anything but that answer keeps the busy tone** (an unknown language, no answer within
``SVP_BUSY_REPORT_TIMEOUT_MS`` - 1500 by default -, an error status, a hand-back the edge
refused): a caller is never left with nothing. The caller's number is never sent or logged.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

import httpx
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
MAILBOX_CONTEXT = "svp-busy"
MAILBOX_LANGUAGES = ("fr", "de", "it", "en")
DEFAULT_REPORT_TIMEOUT_MS = 1500


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


# --- E2c: the caller over the limit leaves a message (P-55 amended) -----------------------------


def voicemail_enabled() -> bool:
    return enabled() and os.getenv("SVP_BUSY_VOICEMAIL", "").strip().lower() in (
        "on",
        "1",
        "true",
    )


def busy_url() -> str:
    """Where to report a caller over the limit: ``SVP_BUSY_REPORT_URL``, else the platform's
    screening address with ``/calls/screen`` read as ``/calls/busy`` (one setting less)."""
    explicit = os.getenv("SVP_BUSY_REPORT_URL", "").strip()
    if explicit:
        return explicit
    screen = os.getenv("SVP_CALLER_SCREEN_URL", "").strip()
    if screen.endswith("/calls/screen"):
        return screen[: -len("screen")] + "busy"
    return ""


def _report_timeout() -> float:
    raw = os.getenv("SVP_BUSY_REPORT_TIMEOUT_MS", "").strip()
    try:
        ms = int(raw) if raw else DEFAULT_REPORT_TIMEOUT_MS
    except ValueError:
        ms = DEFAULT_REPORT_TIMEOUT_MS
    return min(max(ms, 100), 5000) / 1000


async def offer_voicemail(
    *,
    organization_id: int,
    provider: str,
    called_number: Any,
    carrier_call_id: Any,
) -> str | None:
    """The mailbox's language when the platform offers a caller over the limit a message;
    None means the busy tone (the switch off, no address, no answer, anything else)."""
    if not voicemail_enabled():
        return None
    url = busy_url()
    token = os.getenv("SVP_KEY_SERVICE_TOKEN", "").strip()
    if not url or not token:
        logger.warning(
            "P-55: no busy report address or no token - the caller hears busy"
        )
        return None
    body = {
        "organization_id": int(organization_id),
        "provider": provider,
        "called_number": str(called_number or "").strip()[:64] or None,
        "carrier_call_id": str(carrier_call_id or "").strip()[:64] or None,
    }
    try:
        async with httpx.AsyncClient(timeout=_report_timeout()) as client:
            response = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        if response.status_code >= 300:
            logger.warning(
                f"P-55: the busy report answered HTTP {response.status_code} for org "
                f"{organization_id}: the caller hears busy"
            )
            return None
        answer = response.json()
    except Exception as e:  # noqa: BLE001 - the busy tone, never a dropped caller
        logger.warning(
            f"P-55: the busy report was not answered for org {organization_id} "
            f"({type(e).__name__}): the caller hears busy"
        )
        return None
    if (
        not isinstance(answer, dict)
        or answer.get("action") != "voicemail"
        or answer.get("language") not in MAILBOX_LANGUAGES
    ):
        logger.info(
            f"P-55: org {organization_id}: no mailbox offered - the caller hears busy"
        )
        return None
    logger.info(
        f"P-55: org {organization_id}: every line in use - mailbox offered "
        f"({answer['language']}, {provider})"
    )
    return str(answer["language"])


def twilio_fallback_response():
    """Twilio's answer when the platform offered the mailbox: a 503, so Twilio calls the
    number's fallback URL - the platform's busy greeting and record (D5b's path)."""
    from fastapi.responses import Response

    return Response(
        status_code=503, content="every line in use", media_type="text/plain"
    )
