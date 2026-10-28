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


# --- P-42: the handoff on Twilio's native path (ADR-060 amendment of 2026-10-27) ---------------


def test_p42_the_destination_hears_the_platforms_whisper_in_the_agents_language(monkeypatch):
    from api.services.telephony.providers.twilio.provider import transfer_twiml

    monkeypatch.setenv("SVP_TWILIO_HANDOFF", "on")
    announced = transfer_twiml(
        "transfer-CA1",
        {"svp_mode": "announced", "svp_whisper": "Un appel vous est passé par l'assistant de Cabinet & Co. Vous êtes en ligne.", "svp_whisper_language": "fr-FR"},
    )  # fmt: skip
    assert '<Say language="fr-FR">Un appel vous est passé' in announced
    assert "Cabinet &amp; Co." in announced  # escaped for TwiML
    assert "You have answered a transfer call" not in announced
    assert '<Conference endConferenceOnExit="true">transfer-CA1</Conference>' in announced
    # blind: no word to the person
    blind = transfer_twiml("transfer-CA1", {"svp_mode": "blind", "svp_whisper": "x", "svp_whisper_language": "fr-FR"})
    assert "<Say" not in blind and "<Conference" in blind
    # unset = stock: Twilio's English sentence
    monkeypatch.delenv("SVP_TWILIO_HANDOFF")
    assert "You have answered a transfer call" in transfer_twiml("transfer-CA1", {"svp_mode": "announced"})


@pytest.mark.asyncio
async def test_p42_the_caller_id_is_the_number_the_caller_dialled(monkeypatch):
    """The configuration lists no from_numbers (bring your own carrier): the handoff dials
    from the business's own number, and the svp_* facts never reach Twilio as fields."""
    from api.services.telephony.providers.twilio import provider as twilio_provider

    class _Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def text(self):
            return ""

    class _Session:
        posted: list[dict] = []

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def post(self, endpoint, data=None, auth=None):
            _Session.posted.append({"endpoint": endpoint, "data": dict(data or {})})
            return _Response()

    monkeypatch.setenv("SVP_TWILIO_HANDOFF", "on")
    monkeypatch.setattr(twilio_provider.aiohttp, "ClientSession", _Session)

    async def endpoints():
        return "https://voice.example.ch", "wss://voice.example.ch"

    monkeypatch.setattr(twilio_provider, "get_backend_endpoints", endpoints)
    _Session.posted.clear()
    sid = "AC" + "0" * 32
    provider = twilio_provider.TwilioProvider({"account_sid": sid, "auth_token": "t", "region": "ie1"})
    with pytest.raises(Exception):
        # stock still needs from_numbers when no caller id is given
        monkeypatch.setenv("SVP_TWILIO_HANDOFF", "")
        await provider.transfer_call("+41791234567", "tr1", "transfer-CA1", 25, svp_mode="announced")
    monkeypatch.setenv("SVP_TWILIO_HANDOFF", "on")
    _Session.posted.clear()
    try:
        await provider.transfer_call(
            "+41791234567", "tr1", "transfer-CA1", 25,
            svp_mode="announced", svp_whisper="Bonjour.", svp_whisper_language="fr-FR",
            svp_from_number="+41215394092",
        )  # fmt: skip
    except Exception:
        pass  # the fake answers no JSON; the request is what the test reads
    [posted] = _Session.posted
    assert posted["data"]["From"] == "+41215394092" and posted["data"]["To"] == "+41791234567"
    assert posted["data"]["Timeout"] == 25
    assert not any(k.startswith("svp_") for k in posted["data"])
    assert '<Say language="fr-FR">Bonjour.</Say>' in posted["data"]["Twiml"]
    assert posted["endpoint"].startswith("https://api.twilio.com/") or posted["endpoint"].startswith("https://api.dublin.ie1.twilio.com/")
    assert posted["data"]["StatusCallback"] == "https://voice.example.ch/api/v1/telephony/transfer-result/tr1"


# --- D4b's Peoplefone half (ADR-060 amendment of 2026-10-28): CF-216, CF-217, P-44 ---------


def _start_frame(external: bool):
    from pipecat.frames.frames import StartFrame

    frame = StartFrame()
    if external:
        from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

        frame.user_turn_strategies = ExternalUserTurnStrategies()
    return frame


@pytest.mark.asyncio
async def test_cf216_under_a_recogniser_that_decides_the_turns_the_press_ends_the_turn():
    """The owner's Peoplefone call of 2026-10-28 (run 89): with Cartesia Ink the keypad's
    transcription sat until the caller spoke. The press now opens and closes a user turn
    around its transcription when the StartFrame carries external turn strategies."""
    from pipecat.frames.frames import UserStartedSpeakingFrame, UserStoppedSpeakingFrame

    proc, pushed = _processor()
    proc.learn_turn_strategies(_start_frame(external=True))
    await proc.process_frame(InputDTMFFrame(button=KeypadEntry.ZERO), FrameDirection.DOWNSTREAM)
    assert [type(f) for f in pushed] == [
        InputDTMFFrame, UserStartedSpeakingFrame, TranscriptionFrame, UserStoppedSpeakingFrame,
    ]  # fmt: skip
    assert pushed[2].finalized and "[KEYPAD]" in pushed[2].text


@pytest.mark.asyncio
async def test_cf216_under_the_stock_turn_detection_the_press_pushes_the_transcription_only():
    proc, pushed = _processor()
    proc.learn_turn_strategies(_start_frame(external=False))
    proc.learn_turn_strategies(TranscriptionFrame("x", "u", "t"))  # not a StartFrame: ignored
    await proc.process_frame(InputDTMFFrame(button=KeypadEntry.ZERO), FrameDirection.DOWNSTREAM)
    assert [type(f) for f in pushed] == [InputDTMFFrame, TranscriptionFrame]


def test_p44_the_originated_leg_carries_the_workspaces_application_for_the_edge():
    """Unset = stock (no variables); on: the Stasis application and the mode, inheritable
    (`__`) down to the Local channel's dialplan leg and what it dials."""
    assert handoff.channel_variables("dograh_abc", "announced") == {}
    with patch.dict(os.environ, {"SVP_ARI_CHANNEL_VARS": "on"}):
        assert handoff.channel_variables("dograh_abc", "announced") == {
            "__SVP_STASIS": "dograh_abc", "__SVP_MODE": "announced",
        }  # fmt: skip
        assert handoff.channel_variables("dograh_abc") == {"__SVP_STASIS": "dograh_abc"}
        assert handoff.channel_variables("") == {} and handoff.channel_variables(None) == {}


class _PubSub:
    """A Redis pub/sub double: what was published before the listener runs is delivered."""

    def __init__(self):
        self.channels: list[str] = []
        self.queue: list[dict] = []
        self.closed = False

    async def subscribe(self, channel):
        self.channels.append(channel)

    async def unsubscribe(self, channel):
        self.channels.remove(channel)

    async def close(self):
        self.closed = True

    async def listen(self):
        while True:
            if self.queue:
                yield self.queue.pop(0)
            else:
                import asyncio

                await asyncio.sleep(0.01)


class _Redis:
    def __init__(self):
        self.ps = _PubSub()

    def pubsub(self):
        return self.ps


@pytest.mark.asyncio
async def test_cf217_a_failure_published_before_the_wait_is_not_missed():
    """Run 89: the edge refused the dial at once, TRANSFER_FAILED was published 7 ms
    before the tool subscribed, and the caller heard the ring tone for the whole timeout.
    The subscription is made before the dial and the wait reads what arrived meanwhile."""
    from api.services.telephony.call_transfer_manager import CallTransferManager
    from api.services.telephony.transfer_event_protocol import TransferEvent, TransferEventType

    redis = _Redis()
    manager = CallTransferManager(redis_client=redis)
    pubsub = await manager.subscribe_transfer("t-1")
    assert pubsub.channels == ["transfer:events:t-1"]
    # the failure lands between the subscription and the wait (the dial refused at once)
    failed = TransferEvent(
        type=TransferEventType.TRANSFER_FAILED, transfer_id="t-1", original_call_sid="c",
        message="Transfer failed: No route to destination", status="failed",
        action="transfer_failed", reason="rejected",
    )  # fmt: skip
    pubsub.queue.append({"type": "message", "data": failed.to_json()})
    event = await manager.wait_for_transfer_completion("t-1", 1.0, pubsub=pubsub)
    assert event is not None and event.type == TransferEventType.TRANSFER_FAILED
    assert event.reason == "rejected" and pubsub.closed and pubsub.channels == []
    # the tool releases the subscription when the provider itself fails
    pubsub2 = await manager.subscribe_transfer("t-2")
    await manager.release_subscription(pubsub2, "t-2")
    assert pubsub2.channels == [] and pubsub2.closed
