"""P-07 (Swiss Voice Platform, ADR-002 / ADR-058; CALL-005): the caller's language heard,
the agent switched - the processor on the transcripts and the recogniser's settings."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.pipecat import language_switch

LANGUAGES = {
    "fr": {"stt_language": "fr-CH", "tts_voice": "fr-FR-DeniseNeural", "tts_language": "fr-FR",
           "instruction": "Réponds en français.", "announce": "Je vous réponds en français."},
    "de": {"stt_language": "de-DE", "tts_voice": "de-CH-LeniNeural", "tts_language": "de-CH",
           "instruction": "Antworte auf Deutsch.", "announce": "Ich antworte Ihnen gern auf Deutsch."},
}  # fmt: skip


class _Engine:
    def __init__(self):
        self.calls: list[tuple] = []

    async def set_caller_language(self, code, *, instruction, announce):
        self.calls.append((code, instruction, announce))


def _processor(engine=None):
    proc = language_switch.LanguageSwitchProcessor(
        languages=LANGUAGES, current="fr", engine=engine, run_id=77
    )
    pushed: list = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    proc.push_frame = push  # type: ignore[method-assign]
    proc._FrameProcessor__started = True  # the frame-processor base checks it
    return proc, pushed


def test_languages_of_reads_the_platforms_table_and_ignores_a_single_language():
    assert language_switch.languages_of({"svp_languages": LANGUAGES}) == LANGUAGES
    assert language_switch.languages_of({"svp_languages": {"fr": LANGUAGES["fr"]}}) == {}
    assert language_switch.languages_of({}) == {}
    assert language_switch.language_code("de-CH") == "de" and language_switch.language_code(None) is None  # fmt: skip
    assert not language_switch.enabled()
    with patch.dict(os.environ, {"SVP_LANGUAGE_SWITCH": "on"}):
        assert language_switch.enabled()


async def test_p07_a_final_transcript_in_another_admitted_language_switches_once():
    engine = _Engine()
    proc, pushed = _processor(engine)
    with patch("api.services.svp_call_events.notify") as notify:
        # an interim transcript in German changes nothing
        await proc.process_frame(
            InterimTranscriptionFrame("Guten Tag", "u", "t", language="de-DE"), FrameDirection.DOWNSTREAM
        )  # fmt: skip
        assert proc.current == "fr" and engine.calls == []
        # a final one switches: the voice, the engine, the platform - once
        await proc.process_frame(
            TranscriptionFrame("Guten Tag, ich möchte einen Termin.", "u", "t", language="de-DE"),
            FrameDirection.DOWNSTREAM,
        )
        assert proc.current == "de" and proc.switches == ["de"]
        voices = [f for f, _ in pushed if isinstance(f, TTSUpdateSettingsFrame)]
        assert len(voices) == 1
        assert voices[0].settings == {"voice": "de-CH-LeniNeural", "language": "de-CH"}
        assert engine.calls == [("de", "Antworte auf Deutsch.", "Ich antworte Ihnen gern auf Deutsch.")]
        notify.assert_called_once_with("language_switched", 77, language="de", previous_language="fr")
        # the transcript itself went on downstream, after the switch
        assert isinstance(pushed[-1][0], TranscriptionFrame)
        # German again: nothing more; French again: the switch back
        await proc.process_frame(
            TranscriptionFrame("Um zehn Uhr.", "u", "t", language="de-DE"), FrameDirection.DOWNSTREAM
        )  # fmt: skip
        assert proc.switches == ["de"] and notify.call_count == 1
        await proc.process_frame(
            TranscriptionFrame("Merci, en français alors.", "u", "t", language="fr-CH"),
            FrameDirection.DOWNSTREAM,
        )
        assert proc.current == "fr" and proc.switches == ["de", "fr"]
        assert engine.calls[-1][0] == "fr"


async def test_p07_a_language_the_agent_does_not_answer_in_is_left_alone():
    engine = _Engine()
    proc, pushed = _processor(engine)
    with patch("api.services.svp_call_events.notify") as notify:
        await proc.process_frame(
            TranscriptionFrame("Buongiorno, vorrei un appuntamento.", "u", "t", language="it-IT"),
            FrameDirection.DOWNSTREAM,
        )
        await proc.process_frame(TranscriptionFrame("", "u", "t", language="de-DE"), FrameDirection.DOWNSTREAM)  # fmt: skip
        await proc.process_frame(TranscriptionFrame("Bonjour", "u", "t", language=None), FrameDirection.DOWNSTREAM)  # fmt: skip
    assert proc.current == "fr" and proc.switches == [] and engine.calls == []
    assert not any(isinstance(f, TTSUpdateSettingsFrame) for f, _ in pushed)
    notify.assert_not_called()


def test_p07_the_azure_recogniser_identifies_the_language_only_with_two_or_more():
    from pipecat.services.azure.stt import AzureSTTService, AzureSTTSettings
    from pipecat.transcriptions.language import Language

    one = AzureSTTService(
        api_key="k", region="switzerlandnorth",
        settings=AzureSTTSettings(language=Language.FR_CH, languages=["fr-CH"]),
    )  # fmt: skip
    assert one._auto_detect_config is None  # stock recognition in one language
    two = AzureSTTService(
        api_key="k", region="switzerlandnorth",
        settings=AzureSTTSettings(language=Language.FR_CH, languages=["fr-CH", "de-DE"]),
    )  # fmt: skip
    assert two._auto_detect_config is not None
    from azure.cognitiveservices.speech import PropertyId

    assert two._speech_config.get_property(PropertyId.SpeechServiceConnection_LanguageIdMode) == "Continuous"  # fmt: skip
    stock = AzureSTTService(api_key="k", region="switzerlandnorth", settings=AzureSTTSettings(language=Language.FR_CH))  # fmt: skip
    assert stock._auto_detect_config is None


def test_p07_the_engine_appends_the_callers_language_to_every_step_prompt():
    from api.services.workflow.pipecat_engine import PipecatEngine

    engine = PipecatEngine.__new__(PipecatEngine)
    engine._svp_caller_language = None
    engine._svp_language_instruction = ""
    engine._svp_language_announce = ""
    assert engine._svp_with_language("[ROLE] x") == "[ROLE] x"
    engine._svp_caller_language, engine._svp_language_instruction = "de", "Antworte auf Deutsch."
    engine._svp_language_announce = "Ich antworte Ihnen gern auf Deutsch."
    text = engine._svp_with_language("[ROLE] x")
    assert text.startswith("[ROLE] x\n\n[CALLER LANGUAGE]")
    assert "The caller speaks de. Antworte auf Deutsch." in text
    assert "Open your very next reply with exactly this sentence, once" in text
    assert "Ich antworte Ihnen gern auf Deutsch." in text
