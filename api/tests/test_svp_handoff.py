"""P-40 (Swiss Voice Platform, ADR-002 / ADR-060; TEL-008, TEL-010, CALL-003): the human
handoff - the keypad shortcut, the edge's dial string, the failure codes, the platform's
events. Everything behind SVP_HANDOFF / SVP_ARI_TRANSFER_DIAL; unset = stock."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import InputDTMFFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection

from api.services import svp_call_events
from api.services.pipecat import handoff

CONFIG = {"svp_handoff": {"dtmf": "0", "tool": "transfer_to_madame_favre_1a2b3c4d", "destination": "Madame Favre"}}


def test_handoff_of_reads_the_platforms_configuration_and_nothing_else():
    assert handoff.handoff_of(CONFIG) == {
        "tool": "transfer_to_madame_favre_1a2b3c4d", "destination": "Madame Favre", "dtmf": "0",
    }  # fmt: skip
    assert handoff.handoff_of({}) is None and handoff.handoff_of(None) is None
    assert handoff.handoff_of({"svp_handoff": {"dtmf": "0"}}) is None  # no tool: nothing
    # no digit, or a digit that is not one keypad key: no keypad shortcut
    assert handoff.handoff_of({"svp_handoff": {"tool": "t", "dtmf": None}})["dtmf"] == ""
    assert handoff.handoff_of({"svp_handoff": {"tool": "t", "dtmf": "12"}})["dtmf"] == ""
    assert handoff.handoff_of({"svp_handoff": {"tool": "t", "dtmf": "#"}})["dtmf"] == "#"
    assert not handoff.enabled()
    with patch.dict(os.environ, {"SVP_HANDOFF": "on"}):
        assert handoff.enabled()


def _processor():
    proc = handoff.HandoffKeypadProcessor(digit="0", tool="transfer_to_x", run_id=77)
    pushed: list = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    proc.push_frame = push  # type: ignore[method-assign]
    proc.broadcast_interruption = AsyncMock()  # type: ignore[method-assign]
    proc._FrameProcessor__started = True  # the frame-processor base checks it
    return proc, pushed


@pytest.mark.asyncio
async def test_p40_the_configured_digit_interrupts_and_asks_for_a_person():
    proc, pushed = _processor()
    await proc.process_frame(InputDTMFFrame(button=KeypadEntry.ZERO), FrameDirection.DOWNSTREAM)
    assert proc.presses == 1
    proc.broadcast_interruption.assert_awaited_once()
    assert isinstance(pushed[0], InputDTMFFrame)  # the frame goes on first, as pipecat does
    text = [f for f in pushed if isinstance(f, TranscriptionFrame)]
    assert len(text) == 1 and text[0].finalized
    assert text[0].text == (
        "[KEYPAD] The caller pressed 0: they ask for a person. Call transfer_to_x now, without a word."
    )


@pytest.mark.asyncio
async def test_p40_another_digit_and_other_frames_pass_untouched():
    proc, pushed = _processor()
    await proc.process_frame(InputDTMFFrame(button=KeypadEntry.FIVE), FrameDirection.DOWNSTREAM)
    spoken = TranscriptionFrame("Bonjour", "u", "t")
    await proc.process_frame(spoken, FrameDirection.DOWNSTREAM)
    assert proc.presses == 0
    proc.broadcast_interruption.assert_not_awaited()
    assert [type(f) for f in pushed] == [InputDTMFFrame, TranscriptionFrame]
    assert pushed[1] is spoken


def test_p40_the_dial_string_is_the_edges_only_with_the_template(monkeypatch):
    monkeypatch.delenv("SVP_ARI_TRANSFER_DIAL", raising=False)
    assert handoff.svp_dial_string("+41791234567", "announced") == "PJSIP/+41791234567"  # stock
    monkeypatch.setenv("SVP_ARI_TRANSFER_DIAL", "Local/{destination}@svp-transfer-{mode}")
    assert handoff.svp_dial_string("+41791234567", "announced") == "Local/+41791234567@svp-transfer-announced"  # fmt: skip
    assert handoff.svp_dial_string("+41791234567", "blind") == "Local/+41791234567@svp-transfer-blind"  # fmt: skip
    # no mode, or one the edge does not know: announced (the owner's default)
    assert handoff.svp_dial_string("+41791234567", None).endswith("svp-transfer-announced")
    assert handoff.svp_dial_string("+41791234567", "warm").endswith("svp-transfer-announced")
    # a destination already written as a channel is left alone, template or not
    assert handoff.svp_dial_string("PJSIP/softphone2", "blind") == "PJSIP/softphone2"
    assert handoff.svp_dial_string("Local/s@svp-answerer", "blind") == "Local/s@svp-answerer"


def test_p40_the_failure_codes_the_platform_acts_on():
    assert handoff.failure_code("timeout") == "timeout"
    assert handoff.failure_code("textchat_not_supported") == "textchat_not_supported"
    # Asterisk's hangup causes, as the ARI manager words them
    assert handoff.failure_code("The transfer call was not answered. The person may be busy or unavailable right now.") == "no_answer"  # fmt: skip
    assert handoff.failure_code("The person declined the call or their line is busy.") == "busy"
    assert handoff.failure_code("The transfer call failed to connect. There may be a network issue or the number is unavailable.") == "rejected"  # fmt: skip
    assert handoff.failure_code("Transfer failed: Normal Clearing") == "other"
    assert handoff.failure_code(None, "") == "other"


def test_p40_the_platform_is_told_the_three_moments():
    with patch("api.services.svp_call_events.notify") as notify:
        svp_call_events.transfer_started(77, destination="Madame Favre", mode="announced", commit_to_dial_seconds=0.412)  # fmt: skip
        svp_call_events.transferred(77, destination="Madame Favre")
        svp_call_events.transfer_failed(77, destination="Madame Favre", reason="no_answer")
    kinds = [c.args[0] for c in notify.call_args_list]
    assert kinds == ["transfer_started", "transferred", "transfer_failed"]
    assert notify.call_args_list[0].kwargs == {
        "destination": "Madame Favre", "mode": "announced", "commit_to_dial_seconds": 0.412,
    }  # fmt: skip
    assert notify.call_args_list[2].kwargs == {"destination": "Madame Favre", "reason": "no_answer"}
    # the number is never part of it: only the name the business gave
    for call in notify.call_args_list:
        assert "+41" not in str(call)
