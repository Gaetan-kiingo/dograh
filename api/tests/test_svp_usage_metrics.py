"""P-54 (Swiss Voice Platform, ADR-002; E1b, ADR-082; BIL-003, PROV-015, OBS-002): a call's
listening time is kept with its usage. pipecat emits the audio seconds sent to the
recogniser as `STTUsageMetricsData`; with `SVP_STT_USAGE=on` the run's `usage_info` sums
them per processor|||model under `stt`, like the TTS characters. Unset = stock: `stt` {}."""

import os
from unittest.mock import patch

import pytest
from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import (
    LLMTokenUsage,
    LLMUsageMetricsData,
    STTUsage,
    STTUsageMetricsData,
    TTSUsageMetricsData,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.pipecat.pipeline_metrics_aggregator import PipelineMetricsAggregator

AZURE_STT = "AzureSTTService#0"
AZURE_TTS = "AzureTTSService#2"
LLM = "OpenAILLMService#1"


def _aggregator() -> tuple[PipelineMetricsAggregator, list]:
    agg = PipelineMetricsAggregator()
    pushed: list = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    agg.push_frame = push  # type: ignore[method-assign]
    agg._FrameProcessor__started = True  # the frame-processor base checks it
    return agg, pushed


def _stt(
    seconds: float, processor: str = AZURE_STT, model: str = ""
) -> STTUsageMetricsData:
    return STTUsageMetricsData(
        processor=processor, model=model, value=STTUsage(audio_seconds=seconds)
    )


def _call_metrics() -> list[MetricsFrame]:
    """One call's metrics as pipecat emits them: a final transcript's seconds per turn, the
    flush at stop, a second recogniser (the language switch's), the LLM and the voice."""
    return [
        MetricsFrame(data=[_stt(2.1234)]),
        MetricsFrame(
            data=[
                LLMUsageMetricsData(
                    processor=LLM,
                    model="deepseek-v4-flash",
                    value=LLMTokenUsage(
                        prompt_tokens=1200, completion_tokens=40, total_tokens=1240
                    ),
                )
            ]
        ),
        MetricsFrame(
            data=[TTSUsageMetricsData(processor=AZURE_TTS, model="", value=57)]
        ),
        MetricsFrame(data=[_stt(3.5)]),
        MetricsFrame(data=[_stt(0.0004)]),  # the flush at stop: trailing audio
        MetricsFrame(
            data=[_stt(1.25, processor="CartesiaSTTService#3", model="ink-2")]
        ),
        MetricsFrame(
            data=[TTSUsageMetricsData(processor=AZURE_TTS, model="", value=40)]
        ),
    ]


async def _run(agg: PipelineMetricsAggregator) -> None:
    for frame in _call_metrics():
        await agg.process_frame(frame, FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_p54_the_recognisers_seconds_are_summed_per_processor_and_model():
    agg, pushed = _aggregator()
    with patch.dict(os.environ, {"SVP_STT_USAGE": "on"}):
        await _run(agg)
    usage = agg.get_all_usage_metrics_serialized()
    assert usage["stt"] == {
        f"{AZURE_STT}|||": 5.624,  # 2.1234 + 3.5 + 0.0004, to the millisecond
        "CartesiaSTTService#3|||ink-2": 1.25,
    }
    # every frame still flows on
    assert len(pushed) == len(_call_metrics())


@pytest.mark.asyncio
async def test_p54_unset_the_stt_block_stays_empty():
    agg, pushed = _aggregator()
    with patch.dict(os.environ, {"SVP_STT_USAGE": ""}):
        await _run(agg)
    assert agg.get_all_usage_metrics_serialized()["stt"] == {}
    assert len(pushed) == len(_call_metrics())
    agg2, _ = _aggregator()
    with patch.dict(os.environ, {"SVP_STT_USAGE": "off"}):
        await _run(agg2)
    assert agg2.get_all_usage_metrics_serialized()["stt"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["on", ""])
async def test_p54_llm_and_tts_are_untouched(switch):
    agg, _ = _aggregator()
    with patch.dict(os.environ, {"SVP_STT_USAGE": switch}):
        await _run(agg)
    usage = agg.get_all_usage_metrics_serialized()
    assert usage["tts"] == {f"{AZURE_TTS}|||": 97}
    assert usage["llm"] == {
        f"{LLM}|||deepseek-v4-flash": {
            "prompt_tokens": 1200,
            "completion_tokens": 40,
            "total_tokens": 1240,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
        }
    }
    assert set(usage) == {"llm", "tts", "stt", "call_duration_seconds"}
