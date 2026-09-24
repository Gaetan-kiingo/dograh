"""P-18 (Swiss Voice Platform): the progress phrase on a slow tool."""

import asyncio

import pytest

from api.services.workflow import tool_filler


def _config(language):
    return {"body_template": {"call": {"language": language}}}


def test_the_filler_speaks_the_calls_language_and_french_when_unknown():
    assert tool_filler.filler_text(_config("de")).startswith("Einen Moment")
    assert tool_filler.filler_text(_config("it")).startswith("Un attimo")
    assert tool_filler.filler_text(_config("xx")).startswith("Un instant")
    assert tool_filler.filler_text({}).startswith("Un instant")


def test_unset_means_stock_behaviour(monkeypatch):
    monkeypatch.delenv("SVP_TOOL_FILLER_AFTER_MS", raising=False)
    assert tool_filler.filler_after_ms() == 0
    monkeypatch.setenv("SVP_TOOL_FILLER_AFTER_MS", "2000")
    assert tool_filler.filler_after_ms() == 2000
    monkeypatch.setenv("SVP_TOOL_FILLER_AFTER_MS", "nope")
    assert tool_filler.filler_after_ms() == 0


@pytest.mark.asyncio
async def test_a_slow_tool_gets_one_filler_and_a_fast_one_none():
    spoken = []

    async def speak(text):
        spoken.append(text)

    async def slow():
        await asyncio.sleep(0.15)
        return {"ok": True}

    async def fast():
        return {"ok": True}

    assert await tool_filler.with_filler(slow(), speak, "Un instant", after_ms=50) == {"ok": True}
    assert spoken == ["Un instant"]
    assert await tool_filler.with_filler(fast(), speak, "Un instant", after_ms=50) == {"ok": True}
    assert spoken == ["Un instant"]  # nothing more


@pytest.mark.asyncio
async def test_a_failing_filler_never_fails_the_tool():
    async def speak(text):
        raise RuntimeError("tts down")

    async def slow():
        await asyncio.sleep(0.1)
        return "done"

    assert await tool_filler.with_filler(slow(), speak, "x", after_ms=20) == "done"
