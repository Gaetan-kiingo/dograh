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
   **Amended 2026-11-06 (CF-218, ADR-078 - real callers):** a press during the agent's
   OPENING never interrupts it - the AI disclosure is always heard to its end - and is held
   until the first ``BotStoppedSpeakingFrame`` (the moment pipecat's
   ``MuteUntilFirstBotCompleteUserMuteStrategy`` lifts the user mute), then delivered; a
   press during a TOOL CALL (``FunctionCallUserMuteStrategy``'s mute) is held until the call
   answers, so a write in flight is never cancelled by it. Held, it was dropped by the muted
   aggregator before (the owner's run 92, 2026-10-28). A held press is delivered after
   ``SVP_HANDOFF_HOLD_MAX_MS`` (20000) whatever happens, so it is never lost.
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

import asyncio
import os
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InputDTMFFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.time import time_now_iso8601

STOCK_DIAL = "PJSIP/{destination}"
EXTERNAL_TURN_STRATEGIES = "ExternalUserTurnStrategies"
CHANNEL_PREFIXES = ("SIP/", "PJSIP/", "Local/")
MODES = ("announced", "blind")


def enabled() -> bool:
    return os.environ.get("SVP_HANDOFF", "").strip().lower() in ("on", "1", "true")


def hold_max_seconds() -> float:
    """CF-218: the longest a press is held while the opening or a tool call runs."""
    raw = os.environ.get("SVP_HANDOFF_HOLD_MAX_MS", "").strip()
    try:
        ms = int(raw) if raw else 20000
    except ValueError:
        ms = 20000
    return min(max(ms, 1000), 120000) / 1000


def channel_vars_enabled() -> bool:
    """P-44 (ADR-060 amendment of 2026-10-28, D4b's Peoplefone half): unset = stock."""
    return os.environ.get("SVP_ARI_CHANNEL_VARS", "").strip().lower() in ("on", "1", "true")


def channel_variables(
    stasis_app: str | None,
    mode: str | None = None,
    *,
    trunk: str | None = None,
    caller_id: str | None = None,
    run_id: int | str | None = None,
    organization_id: int | str | None = None,
) -> dict[str, str]:
    """What the edge's dialplan learns about a leg the runtime originates: the Stasis
    application it dials for (the workspace's trunk and caller id are globals named after
    it on the edge), the handoff's mode and - amended 2026-10-29, several lines per
    workspace - the trunk the call came in on with the number the caller dialled as the
    caller id (`SVP_TRUNK` read from the inbound channel at the answer, `called_number`),
    so the handoff goes out on the line it came in on. Amended 2026-11-11 (E1b, ADR-082):
    the leg names its call - the workflow run and the runtime organisation - so the edge's
    hangup handler tells the platform how long the dialled leg lasted and whose call it is.
    The `__` prefix makes Asterisk hand the variables down to the Local channel's dialplan
    leg and to what it dials."""
    if not channel_vars_enabled() or not stasis_app:
        return {}
    out = {"__SVP_STASIS": str(stasis_app)}
    if mode:
        out["__SVP_MODE"] = str(mode)
    if trunk:
        out["__SVP_TRUNK"] = str(trunk)
        if caller_id:
            out["__SVP_CALLERID"] = str(caller_id)
    if run_id not in (None, ""):
        out["__SVP_RUN"] = str(run_id)
    if organization_id not in (None, ""):
        out["__SVP_ORG"] = str(organization_id)
    return out


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
    # a carrier's refusal of the dial (SIP 4xx/5xx Asterisk maps to « Interworking »): the
    # owner's trunk answered 402 Payment Required on 2026-10-28 (no credit on the account)
    if "interworking" in text or "refused" in text or "sip 4" in text or "sip 5" in text:
        return "rejected"
    return "other"


class HandoffKeypadProcessor(FrameProcessor):
    """The configured keypad digit becomes the caller's request for a person (part 1)."""

    def __init__(
        self,
        *,
        digit: str,
        tool: str,
        run_id: int | None,
        external_turns: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._digit = digit
        self._tool = tool
        self._run_id = run_id
        self.presses = 0
        # CF-216 (2026-10-28, the owner's Peoplefone calls): a recogniser that decides the
        # turns itself (Cartesia Ink, ExternalUserTurnStrategies) makes the aggregator infer
        # only on its own turn end - the keypad's transcription sat until the caller spoke.
        # Under those strategies the press ends the turn itself. The pipeline says so when
        # it builds this processor (`uses_external_turns`); a frame carrying the recogniser's
        # recommended strategies (its service metadata frame) says so too.
        self._external_turns = bool(external_turns)
        # CF-218 (2026-11-06): the opening is over at the first BotStoppedSpeakingFrame (the
        # aggregator's own mute lifts on it); tool calls in flight are tracked by their ids
        # from the frames the LLM service broadcasts upstream
        self._opening_done = False
        self._calls: set[str] = set()
        self._held: FrameDirection | None = None
        self._hold_task: asyncio.Task | None = None
        self.held_presses = 0

    def _blocked(self) -> str | None:
        if not self._opening_done:
            return "the opening"
        if self._calls:
            return "a tool call"
        return None

    def _track(self, frame: Frame) -> None:
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._opening_done = True
        elif isinstance(frame, FunctionCallsStartedFrame):
            for call in getattr(frame, "function_calls", None) or []:
                self._calls.add(str(getattr(call, "tool_call_id", "")))
        elif isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
            self._calls.discard(str(getattr(frame, "tool_call_id", "")))

    async def _deliver(self, direction: FrameDirection) -> None:
        """The press as the caller's request: the agent interrupted, the platform's words."""
        await self.broadcast_interruption()
        if self._external_turns:
            await self.push_frame(UserStartedSpeakingFrame(), direction)
        await self.push_frame(
            TranscriptionFrame(
                text=keypad_text(self._digit, self._tool),
                user_id="",
                timestamp=time_now_iso8601(),
                finalized=True,
            ),
            direction,
        )
        if self._external_turns:
            await self.push_frame(UserStoppedSpeakingFrame(), direction)

    async def _release(self, reason: str) -> None:
        direction, self._held = self._held, None
        task, self._hold_task = self._hold_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if direction is None:
            return
        logger.info(f"P-40: the held keypad {self._digit} delivered on run {self._run_id} ({reason})")
        await self._deliver(direction)

    async def _hold_timeout(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self._held is not None:
            logger.warning(
                f"P-40: keypad {self._digit} held {seconds:.0f} s on run {self._run_id} "
                f"({self._blocked()} not over): delivered anyway"
            )
            await self._release("the hold's time limit")

    async def cleanup(self):
        task, self._hold_task = self._hold_task, None
        if task is not None:
            task.cancel()
        await super().cleanup()

    def learn_turn_strategies(self, frame: Frame) -> None:
        strategies = getattr(frame, "user_turn_strategies", None)
        if strategies is not None and type(strategies).__name__ == EXTERNAL_TURN_STRATEGIES:
            self._external_turns = True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self.learn_turn_strategies(frame)
        if isinstance(frame, InputDTMFFrame) and frame.button.value == self._digit:
            self.presses += 1
            logger.info(
                f"P-40: keypad {self._digit} pressed on run {self._run_id}: a person asked for "
                f"({self._tool}; turn ended by the press: {self._external_turns})"
            )
            await self.push_frame(frame, direction)
            blocked = self._blocked()
            if blocked:
                # CF-218: never cut the opening (the AI disclosure) nor a tool call in flight;
                # held - one delivery however many presses - until it is over
                self.held_presses += 1
                if self._held is None:
                    self._held = direction
                    self._hold_task = asyncio.get_running_loop().create_task(
                        self._hold_timeout(hold_max_seconds())
                    )
                    logger.info(
                        f"P-40: keypad {self._digit} held on run {self._run_id} until {blocked} is over"
                    )
                return
            await self._deliver(direction)
            return
        self._track(frame)
        await self.push_frame(frame, direction)
        if isinstance(frame, (EndFrame, CancelFrame)):
            return
        if self._held is not None and self._blocked() is None:
            await self._release("the opening or the tool call over")
