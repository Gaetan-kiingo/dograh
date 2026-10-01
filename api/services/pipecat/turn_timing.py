"""P-28 (Swiss Voice Platform, ADR-002 / ADR-046; SRS PERF-003, OBS-002): the marks of a
turn's timing, kept with the run.

Stock Dograh keeps a timeline with every run (``logs.realtime_feedback_events``): the
latency from the end of the caller's speech to the first sound, the model's first chunk,
tool start and end, step changes. Four facts go to the live page only or are dropped:
when the bot starts and stops speaking, the first byte of the speech-to-text and
text-to-speech services, and which spoken sentence was the progress phrase (P-18). With
``SVP_TURN_TIMING=on`` the feedback observer appends them to the same timeline as
``svp-timing`` events - times and names only, never text. A type of our own: stock
readers ignore it, so their figures do not change. Unset = stock behaviour.
"""

from __future__ import annotations

import os
from typing import Any

EVENT_TYPE = "svp-timing"

BOT_STARTED = "bot_started"
BOT_STOPPED = "bot_stopped"
FIRST_BYTE = "first_byte"
PROGRESS_PHRASE = "progress_phrase"


def enabled() -> bool:
    return os.getenv("SVP_TURN_TIMING", "").strip().lower() == "on"


def service_of(processor: str | None) -> str | None:
    """« stt » or « tts » from a processor's name (« AzureSTTService#1 »); None for the
    model, whose first chunk stock Dograh already keeps, and for anything else."""
    name = processor or ""
    if "STT" in name:
        return "stt"
    if "TTS" in name:
        return "tts"
    return None


def mark(name: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"mark": name}
    payload.update({key: value for key, value in fields.items() if value is not None})
    return {"type": EVENT_TYPE, "payload": payload}
