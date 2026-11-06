"""P-51 (Swiss Voice Platform, ADR-002; ADR-071 amendment of 2026-11-06; OPS-001, SEC-013,
CF-249, CF-209): a blocked caller is refused before the call is answered, and every inbound
call says how far its caller-ID can be trusted.

With ``SVP_CALLER_SCREEN_URL`` set, the runtime asks the platform, before an inbound call
gets a workflow run, whether the caller is on a list - the workspace's own or Driing's -
with the bearer ``SVP_KEY_SERVICE_TOKEN`` (its credential to the platform, P-17/P-33)::

    POST {"organization_id", "caller_number", "called_number", "provider", "caller_trust"}
    200  {"action": "allow" | "reject", "list": null | "workspace" | "platform"}

On ``reject`` nothing of the call starts: no run, no answer, no engine. The ARI path hangs
the unanswered channel up with cause 21 (call rejected), the Twilio path answers
``<Reject reason="rejected"/>``; the caller hears the carrier's refusal, never a word of
ours (the owner, 2026-11-06).

**Fail open.** A screening the platform does not answer in time
(``SVP_CALLER_SCREEN_TIMEOUT_MS``, 800 by default), a refused connection, an error status or
a body that is not the answer above lets the call through, logged at WARNING: a screening
outage must never drop every call - the lists protect against a few numbers, the line
serves everyone else.

**The caller-ID's trust** (SEC-013): ``network`` when the carrier asserted the identity
(the edge's dialplan read a P-Asserted-Identity on the trunk; Twilio's ``StirVerstat``
passed with full attestation), ``withheld`` when the number is hidden or empty,
``presented`` otherwise - a From anyone can write. The edge says it in the channel variable
``SVP_CALLER_TRUST``; without it the runtime classes the number itself. It rides the run's
``initial_context`` (``svp_caller_trust``) and P-33's events (``caller_trust``).

The caller's number never appears in a log line of this module. Unset = stock behaviour:
nothing is asked, nothing is classed.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from loguru import logger

DEFAULT_TIMEOUT_MS = 800
TRUST_VALUES = ("network", "presented", "withheld")
# what carriers write in the From of a call whose number is hidden (Twilio: "anonymous",
# "+266696687" - "ANONYMOUS" on a keypad -, "restricted", "unknown")
WITHHELD_FROMS = frozenset(
    {
        "",
        "anonymous",
        "restricted",
        "unknown",
        "private",
        "unavailable",
        "+266696687",
        "266696687",
    }
)
REJECT_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response><Reject reason="rejected"/></Response>'


def screen_url() -> str:
    return os.getenv("SVP_CALLER_SCREEN_URL", "").strip()


def enabled() -> bool:
    return bool(screen_url())


def timeout_seconds() -> float:
    raw = os.getenv("SVP_CALLER_SCREEN_TIMEOUT_MS", "").strip()
    try:
        ms = int(raw) if raw else DEFAULT_TIMEOUT_MS
    except ValueError:
        ms = DEFAULT_TIMEOUT_MS
    return min(max(ms, 50), 5000) / 1000


def _has_digits(value: Any) -> bool:
    return any(ch.isdigit() for ch in str(value or ""))


def number_or_none(caller_number: Any) -> str | None:
    """What is sent as the caller's number: the number when it has digits, else nothing."""
    text = str(caller_number or "").strip()
    if text.lower() in WITHHELD_FROMS or not _has_digits(text):
        return None
    return text[:64]


def trust_of(caller_number: Any, raw: Any = None) -> str:
    """The edge's word when it said one; otherwise withheld for a hidden or empty number,
    presented for any other (nothing here can say the carrier asserted it)."""
    said = str(raw or "").strip().lower()
    if said in TRUST_VALUES:
        return said
    return "presented" if number_or_none(caller_number) else "withheld"


def trust_from_twilio(form: dict[str, Any] | None) -> str:
    """Twilio's inbound webhook: a hidden From is withheld; a STIR/SHAKEN full attestation
    (``StirVerstat`` = TN-Validation-Passed-A) is the network's word; the rest presented."""
    form = form or {}
    from_raw = str(form.get("From") or "").strip()
    if number_or_none(from_raw) is None:
        return "withheld"
    if str(form.get("StirVerstat") or "").startswith("TN-Validation-Passed-A"):
        return "network"
    return "presented"


async def screen(
    *,
    organization_id: int,
    caller_number: Any,
    called_number: Any,
    provider: str,
    caller_trust: str,
) -> bool:
    """True when the platform says to refuse the call; False otherwise - including every
    failure (fail open, logged without the number)."""
    url = screen_url()
    if not url:
        return False
    token = os.getenv("SVP_KEY_SERVICE_TOKEN", "").strip()
    if not token:
        logger.warning(
            "P-51: SVP_CALLER_SCREEN_URL is set but SVP_KEY_SERVICE_TOKEN is not: call let through"
        )
        return False
    body = {
        "organization_id": int(organization_id),
        "caller_number": number_or_none(caller_number),
        "called_number": str(called_number or "").strip()[:64] or None,
        "provider": provider,
        "caller_trust": caller_trust if caller_trust in TRUST_VALUES else "presented",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds()) as client:
            response = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        if response.status_code >= 300:
            logger.warning(
                f"P-51: the screening answered HTTP {response.status_code} for org "
                f"{organization_id}: call let through"
            )
            return False
        answer = response.json()
    except Exception as e:  # noqa: BLE001 - fail open: never a dropped call for a screening
        logger.warning(
            f"P-51: the screening was not answered for org {organization_id} "
            f"({type(e).__name__}): call let through"
        )
        return False
    if not isinstance(answer, dict) or answer.get("action") not in ("allow", "reject"):
        logger.warning(
            f"P-51: the screening's answer is not one for org {organization_id}: call let through"
        )
        return False
    if answer["action"] == "reject":
        logger.info(
            f"P-51: blocked caller refused for org {organization_id}, list "
            f"{answer.get('list') or 'unnamed'} ({provider}, caller-ID {body['caller_trust']})"
        )
        return True
    return False


def twilio_reject_response():
    """Twilio's verb for a call it must not answer: the caller hears the network's refusal."""
    from fastapi.responses import Response

    return Response(content=REJECT_TWIML, media_type="application/xml")
