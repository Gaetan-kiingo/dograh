"""P-18 (Swiss Voice Platform, ADR-002 / ADR-033 point 6; SRS PERF-008): a progress
phrase when a tool is slow.

The prompt asks the model to say "one moment" before calling a tool (ADR-007); that
is a rule, not a guarantee, and it says nothing about a tool that takes four
seconds. With ``SVP_TOOL_FILLER_AFTER_MS`` set (2000 in the platform's compose
override), the engine speaks the filler for the call's language once, when a tool has
not answered after that many milliseconds, then keeps waiting up to the tool's own
timeout. Unset or 0 = stock behaviour (no runtime filler). P-25: the platform may write
the agent's own approved phrase into the workflow's configurations (``svp_phrases.filler``);
it is spoken instead - one tool definition serves every agent of a workspace, so the phrase
cannot live on the tool.
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


def filler_text(config: dict[str, Any] | None, approved: str | None = None) -> str:
    """The phrase spoken when a tool is slow: P-25 (ADR-044 point 1) - the sentence the
    customer approved for this agent, from the run's workflow configurations
    (``svp_phrases.filler``, frozen per run like every configuration) - or the runtime's
    own phrase for the call's language."""
    text = " ".join(str(approved or "").split())
    return text or FILLERS[language_of(config)]


def approved_filler(configurations: dict[str, Any] | None) -> str:
    """P-25: what the platform wrote for this workflow, "" when nothing."""
    phrases = (configurations or {}).get("svp_phrases") or {}
    return " ".join(str(phrases.get("filler") or "").split())


# P-52 (Swiss Voice Platform, ADR-033 amendment of 2026-11-07; PERF-008): a confirmed booking
# says what it is doing at the moment it is sent - `SVP_WRITE_PROGRESS=on` (unset = stock: the
# delayed filler alone). Only the call that carries the read-back's confirmation (the read-back
# itself writes nothing and answers at once), only a write that has a sentence. The owner's
# test: tried on calls, reverted by the switch if it does not sound right.
WRITE_PHRASES = {
    "book_appointment": {
        "fr": "Un instant, j'enregistre votre rendez-vous.",
        "de": "Einen Moment, ich trage Ihren Termin ein.",
        "it": "Un attimo, registro il suo appuntamento.",
        "en": "One moment, I'm booking your appointment.",
    },
}


def write_progress_on() -> bool:
    return os.getenv("SVP_WRITE_PROGRESS", "").strip().lower() in ("on", "1", "true")


def write_progress_text(
    function_name: str, arguments: dict[str, Any] | None, config: dict[str, Any] | None
) -> str | None:
    """The sentence a confirmed write says as it is sent, in the call's language; None when the
    switch is off, when the call is the read-back (no confirmation yet), or when the write has
    no sentence."""
    if not write_progress_on():
        return None
    if not str((arguments or {}).get("confirm") or "").strip():
        return None
    for name, phrases in WRITE_PHRASES.items():
        # the platform names a second connection's tool `book_appointment_2`
        if function_name == name or function_name.startswith(f"{name}_"):
            return phrases[language_of(config)]
    return None


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
