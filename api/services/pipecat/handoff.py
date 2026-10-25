"""P-40 (Swiss Voice Platform, ADR-002 / ADR-060; TEL-008, TEL-010, CALL-003): the human
handoff as the platform needs it, around the stock `transfer_call` tool.

With ``SVP_HANDOFF=on`` and a workflow whose configurations carry ``svp_handoff``::

    {"dtmf": "0", "tool": "transfer_to_madame_favre_1a2b3c4d", "destination": "Madame Favre"}

four things change, each unset = stock:

1. **The keypad shortcut.** The ``HandoffKeypadProcessor`` sits right after the recogniser
   (where P-07's language switch sits). A press of the configured digit (P-39 makes it an
   ``InputDTMFFrame`` on the Asterisk leg; Twilio's media stream carries its own) interrupts
   the agent and pushes one final transcription in the platform's words - « [KEYPAD] The
   caller pressed 0: they ask for a person. Call <tool> now, without a word. » - so the
   model calls the transfer tool at once (pipecat's own DTMF aggregator works the same way:
   a keypad entry becomes a transcription). Any other digit passes untouched.
2. **The dial string is the edge's** (``svp_dial_string``): ``SVP_ARI_TRANSFER_DIAL``, a
   template with ``{destination}`` and ``{mode}`` (« Local/{destination}@svp-transfer-{mode} »),
   turns the platform's E.164 number into what the edge dials - announced (the person hears a
   short recorded announcement before being connected) or blind; the stock ``PJSIP/<number>``
   when unset; a destination already written as a channel is left alone.
3. **The sentence first, then the dial:** the transfer handler waits for the pre-transfer
   message's playback before originating the destination (the stock external-PBX wait),
   so the hold music never plays over the sentence.
4. **The platform told** (P-33's channel): ``transfer_started`` with the destination's name,
   the mode and the seconds from the end of the sentence to the dial (PERF-010),
   ``transferred`` when the destination answered (HUMAN_TRANSFERRED), ``transfer_failed``
   with a reason the platform can act on (``failure_code``).
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger
from pipecat.frames.frames import Frame, InputDTMFFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.time import time_now_iso8601

STOCK_DIAL = "PJSIP/{destination}"
CHANNEL_PREFIXES = ("SIP/", "PJSIP/", "Local/")
MODES = ("announced", "blind")


def enabled() -> bool:
    return os.environ.get("SVP_HANDOFF", "").strip().lower() in ("on", "1", "true")


def handoff_of(configurations: dict[str, Any] | None) -> dict[str, str] | None:
    """The platform's handoff from the run's workflow configurations: the tool's function
    name is required, the digit optional (None = no keypad shortcut)."""
    raw = (configurations or {}).get("svp_handoff")
    if not isinstance(raw, dict) or not str(raw.get("tool") or "").strip():
        return None
    digit = str(raw.get("dtmf") or "").strip()
    out = {
        "tool": str(raw["tool"]).strip(),
        "destination": str(raw.get("destination") or "").strip(),
        "dtmf": digit if digit and len(digit) == 1 and digit in "0123456789*#" else "",
    }
    return out


def keypad_text(digit: str, tool: str) -> str:
    return (
        f"[KEYPAD] The caller pressed {digit}: they ask for a person. "
        f"Call {tool} now, without a word."
    )


def svp_dial_string(destination: str, mode: str | None = None) -> str:
    """What the ARI provider originates for a destination (part 2)."""
    destination = str(destination or "").strip()
    if destination.startswith(CHANNEL_PREFIXES):
        return destination
    template = os.environ.get("SVP_ARI_TRANSFER_DIAL", "").strip()
    if not template:
        return STOCK_DIAL.format(destination=destination)
    chosen = mode if mode in MODES else "announced"
    return template.format(destination=destination, mode=chosen)


# the reasons the platform's `transfer_failed` carries (ADR-060 decision 8); the stock
# handler's own codes pass through, Asterisk's hangup-cause sentences are classified
_KNOWN = {
    "timeout", "provider_error", "no_destination", "execution_error",
    "textchat_not_supported", "webrtc_not_supported", "provider_does_not_support_transfer",
    "no_organization_id", "resolver_error",
}  # fmt: skip


def failure_code(reason: Any, message: Any = "") -> str:
    reason_text = str(reason or "").strip()
    if reason_text in _KNOWN:
        return reason_text
    text = f"{reason_text} {message or ''}".lower()
    if "not answered" in text or "no answer" in text:
        return "no_answer"
    if "busy" in text or "declined" in text:
        return "busy"
    if "rejected" in text or "failed to connect" in text or "unavailable" in text:
        return "rejected"
    return "other"


class HandoffKeypadProcessor(FrameProcessor):
    """The configured keypad digit becomes the caller's request for a person (part 1)."""

    def __init__(self, *, digit: str, tool: str, run_id: int | None, **kwargs):
        super().__init__(**kwargs)
        self._digit = digit
        self._tool = tool
        self._run_id = run_id
        self.presses = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputDTMFFrame) and frame.button.value == self._digit:
            self.presses += 1
            logger.info(
                f"P-40: keypad {self._digit} pressed on run {self._run_id}: a person asked for "
                f"({self._tool})"
            )
            await self.push_frame(frame, direction)
            await self.broadcast_interruption()
            await self.push_frame(
                TranscriptionFrame(
                    text=keypad_text(self._digit, self._tool),
                    user_id="",
                    timestamp=time_now_iso8601(),
                    finalized=True,
                ),
                direction,
            )
            return
        await self.push_frame(frame, direction)
