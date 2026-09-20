"""P-14 (Swiss Voice Platform, ADR-002 / ADR-015): native campaigns switchable off.

This runtime's campaign feature places outbound calls with no notion of lawful
basis, suppression, calling windows, caps or evidence. A deployment whose own
platform owns outbound calling sets NATIVE_CAMPAIGNS=off: the routes answer 404,
the orchestrator does not start, and the worker jobs do nothing. Unset keeps
stock behavior.
"""

import os

from fastapi import HTTPException


def native_campaigns_enabled() -> bool:
    return os.getenv("NATIVE_CAMPAIGNS", "").strip().lower() != "off"


def require_native_campaigns() -> None:
    """Router dependency: closed before any lookup, indistinguishable from absent."""
    if not native_campaigns_enabled():
        raise HTTPException(status_code=404, detail="Not found")
