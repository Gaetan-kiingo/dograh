"""P-28 (Swiss Voice Platform, ADR-002 / ADR-046): the marks of a turn's timing are kept
with the run when SVP_TURN_TIMING=on, and nothing changes when it is not."""

from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    MetricsFrame,
    SVPTimingMarkFrame,
    TTSSpeakFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from api.services.pipecat import turn_timing
from api.services.pipecat.realtime_feedback_observer import RealtimeFeedbackObserver


class _Buffer:
    current_node_id = None
    current_node_name = None

    def __init__(self):
        self.events = []

    async def append(self, event, **_):
        self.events.append(event)


def _pushed(frame):
    return FramePushed(
        source=SimpleNamespace(),
        destination=SimpleNamespace(),
        frame=frame,
        direction=FrameDirection.DOWNSTREAM,
        timestamp=0,
    )


def _first_byte(processor, seconds, model=None):
    return MetricsFrame(
        data=[TTFBMetricsData(processor=processor, model=model, value=seconds)]
    )


async def _observe(frames):
    buffer = _Buffer()
    observer = RealtimeFeedbackObserver(ws_sender=None, logs_buffer=buffer)
    for frame in frames:
        await observer.on_push_frame(_pushed(frame))
    return buffer.events


def _progress_phrase():
    frame = TTSSpeakFrame(
        "Un instant, je vérifie.", append_to_context=False, persist_to_logs=True
    )
    frame.svp_progress = True
    return frame


def _a_turn():
    return [
        _first_byte("AzureSTTService#1", 2.383),
        _first_byte("OpenAILLMService#143", 1.34, "gpt-6-luna"),
        _progress_phrase(),
        _first_byte("AzureTTSService#1", 0.239, "fr-CH-ArianeNeural"),
        BotStartedSpeakingFrame(),
        BotStoppedSpeakingFrame(),
    ]


def test_the_switch_is_off_unless_set_to_on(monkeypatch):
    monkeypatch.delenv("SVP_TURN_TIMING", raising=False)
    assert turn_timing.enabled() is False
    monkeypatch.setenv("SVP_TURN_TIMING", "1")
    assert turn_timing.enabled() is False
    monkeypatch.setenv("SVP_TURN_TIMING", " On ")
    assert turn_timing.enabled() is True


def test_the_service_is_read_from_the_processors_name():
    assert turn_timing.service_of("AzureSTTService#1") == "stt"
    assert turn_timing.service_of("GoogleTTSService#2") == "tts"
    assert turn_timing.service_of("OpenAILLMService#143") is None
    assert turn_timing.service_of(None) is None


@pytest.mark.asyncio
async def test_unset_means_stock_behaviour(monkeypatch):
    monkeypatch.delenv("SVP_TURN_TIMING", raising=False)
    events = await _observe(_a_turn())
    # what stock keeps: the model's first chunk and the engine's sentence
    assert [e["type"] for e in events] == ["rtf-ttfb-metric", "rtf-bot-text"]


@pytest.mark.asyncio
async def test_the_marks_are_kept_with_the_run_when_on(monkeypatch):
    monkeypatch.setenv("SVP_TURN_TIMING", "on")
    events = await _observe(_a_turn())
    marks = [e["payload"] for e in events if e["type"] == turn_timing.EVENT_TYPE]
    assert marks == [
        {"mark": "first_byte", "service": "stt", "seconds": 2.383},
        {"mark": "progress_phrase"},
        {
            "mark": "first_byte",
            "service": "tts",
            "seconds": 0.239,
            "model": "fr-CH-ArianeNeural",
        },
        {"mark": "bot_started"},
        {"mark": "bot_stopped"},
    ]
    # the stock events are still there, once each: stock readers see no difference
    stock = [e["type"] for e in events if e["type"] != turn_timing.EVENT_TYPE]
    assert stock == ["rtf-ttfb-metric", "rtf-bot-text"]


@pytest.mark.asyncio
async def test_a_mark_never_carries_text(monkeypatch):
    monkeypatch.setenv("SVP_TURN_TIMING", "on")
    events = await _observe(_a_turn())
    for event in events:
        if event["type"] == turn_timing.EVENT_TYPE:
            assert "text" not in event["payload"]
            assert "Un instant" not in str(event)


@pytest.mark.asyncio
async def test_a_sentence_of_the_engine_that_is_not_the_progress_phrase_is_not_marked(
    monkeypatch,
):
    monkeypatch.setenv("SVP_TURN_TIMING", "on")
    events = await _observe(
        [TTSSpeakFrame("Je vous transfère.", persist_to_logs=True)]
    )
    assert [e["type"] for e in events] == ["rtf-bot-text"]


@pytest.mark.asyncio
async def test_a_components_timing_mark_is_kept_with_the_run_only_when_on(monkeypatch):
    frame = lambda: SVPTimingMarkFrame(  # noqa: E731
        mark="early_start", data={"mode": "observe", "outcome": "resumed"}
    )
    monkeypatch.delenv("SVP_TURN_TIMING", raising=False)
    assert await _observe([frame()]) == []
    monkeypatch.setenv("SVP_TURN_TIMING", "on")
    [event] = await _observe([frame()])
    assert event == {
        "type": "svp-timing",
        "payload": {"mark": "early_start", "mode": "observe", "outcome": "resumed"},
    }
