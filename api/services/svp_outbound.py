"""P-43 (Swiss Voice Platform, ADR-002 / ADR-015 amendment of 2026-10-27; OUT-025, TEL-015,
CF-028): the platform's door for a call the business places - and the stock doors closed.

``SVP_OUTBOUND_AUTH=on``:
1. ``POST /api/v1/telephony/svp/outbound`` places a call against the PUBLISHED definition
   of a workflow of the API key's organisation, only with the platform's authorisation in
   ``X-SVP-Outbound-Authorization``: ``v1=<expires>.<hmac>`` - an HMAC-SHA256 over
   ``organisation.workflow.number.attempt.expires`` with ``SVP_KEY_SERVICE_TOKEN`` (the
   runtime's credential to the platform, P-17; no new secret). A missing, expired or wrong
   authorisation is 403, before anything is read.
2. The stock single-call triggers (``/public/agent/...`` four routes, ``/telephony/initiate-call``)
   answer 404: a tenant's runtime key alone no longer places a call (A8's finding).
3. The attempt's id rides the run's initial context (``svp_attempt_id``) and every call
   event the platform receives (P-33 amended).
4. ``SVP_ARI_OUTBOUND_DIAL`` (e.g. ``Local/{destination}@svp-outbound``): what the ARI
   provider originates for an E.164 destination - the edge's dialplan decides the trunk and
   the caller id; unset = the stock ``PJSIP/<number>``.

Unset = stock behaviour everywhere.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import HTTPException

HEADER = "X-SVP-Outbound-Authorization"
CHANNEL_PREFIXES = ("PJSIP/", "SIP/", "Local/", "DAHDI/", "IAX2/")


def outbound_auth_enabled() -> bool:
    return os.getenv("SVP_OUTBOUND_AUTH", "").strip().lower() in ("on", "1", "true")


def refuse_stock_trigger() -> None:
    """Part 2: the runtime's own single-call doors, closed before any lookup."""
    if outbound_auth_enabled():
        raise HTTPException(status_code=404, detail="Not found")


def _secret() -> bytes:
    return os.getenv("SVP_KEY_SERVICE_TOKEN", "").strip().encode()


def sign(organization_id: int, workflow_id: int, phone: str, attempt_id: str, expires: int) -> str:
    message = f"{organization_id}.{workflow_id}.{phone}.{attempt_id}.{expires}".encode()
    return f"v1={expires}." + hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def verify_authorization(
    header: str | None, *, organization_id: int, workflow_id: int, phone: str, attempt_id: str
) -> None:
    """Part 1: refuse (403) unless the platform signed exactly this call, and recently."""
    if not _secret():
        raise HTTPException(status_code=403, detail="outbound_not_authorized")
    text = str(header or "").strip()
    if not text.startswith("v1="):
        raise HTTPException(status_code=403, detail="outbound_not_authorized")
    try:
        expires_text, digest = text[3:].split(".", 1)
        expires = int(expires_text)
    except ValueError:
        raise HTTPException(status_code=403, detail="outbound_not_authorized") from None
    if expires < int(time.time()):
        raise HTTPException(status_code=403, detail="outbound_authorization_expired")
    expected = sign(organization_id, workflow_id, phone, attempt_id, expires)
    if not hmac.compare_digest(expected.split(".", 1)[1], digest):
        raise HTTPException(status_code=403, detail="outbound_not_authorized")


def outbound_dial_string(destination: str) -> str:
    """Part 4: what the ARI provider originates for a platform destination."""
    destination = str(destination or "").strip()
    if destination.startswith(CHANNEL_PREFIXES):
        return destination
    template = os.environ.get("SVP_ARI_OUTBOUND_DIAL", "").strip()
    if not template:
        return f"PJSIP/{destination}"
    return template.format(destination=destination)
