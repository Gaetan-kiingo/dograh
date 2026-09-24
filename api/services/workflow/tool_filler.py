"""P-18 (Swiss Voice Platform, ADR-002 / ADR-033 point 6; SRS PERF-008): a progress
phrase when a tool is slow.

The prompt asks the model to say "one moment" before calling a tool (ADR-007); that
is a rule, not a guarantee, and it says nothing about a tool that takes four
seconds. With ``SVP_TOOL_FILLER_AFTER_MS`` set (2000 in the platform's compose
override), the engine speaks the filler for the call's language once, when a tool has
not answered after that many milliseconds, then keeps waiting up to the tool's own
timeout. Unset or 0 = stock behaviour (no runtime filler).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

FILLERS = {
    "fr": "Un instant, je vérifie.",
    "de": "Einen Moment, ich prüfe das.",
    "it": "Un attimo, verifico.",
    "en": "One moment, let me check that.",
}


def filler_after_ms() -> int:
    raw = os.getenv("SVP_TOOL_FILLER_AFTER_MS", "").strip()
    try:
        return max(0, int(raw)) if raw else 0
    except ValueError:
        return 0


def language_of(config: dict[str, Any] | None) -> str:
    """The agent's language as the platform wrote it into the tool's body template
    (``call.language``, C1a); French when absent."""
    call = ((config or {}).get("body_template") or {}).get("call") or {}
    language = str(call.get("language") or "").lower()
    return language if language in FILLERS else "fr"


def filler_text(config: dict[str, Any] | None) -> str:
    return FILLERS[language_of(config)]


async def with_filler(
    work: Awaitable[Any],
    speak: Callable[[str], Awaitable[None]],
    text: str,
    after_ms: int | None = None,
) -> Any:
    """Run ``work``; if it has not finished after ``after_ms``, ``speak(text)`` once,
    then keep waiting for the result. The caller's own timeout still applies."""
    after = filler_after_ms() if after_ms is None else after_ms
    task = asyncio.ensure_future(work)
    if after <= 0:
        return await task
    done, _ = await asyncio.wait({task}, timeout=after / 1000)
    if not done:
        try:
            await speak(text)
        except Exception:  # the filler must never fail the tool
            pass
    return await task
