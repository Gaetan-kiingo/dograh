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


def test_p25_the_agents_approved_phrase_is_spoken_instead_of_the_runtimes():
    config = {"body_template": {"call": {"language": "fr"}}}
    approved = tool_filler.approved_filler({"svp_phrases": {"filler": "  Je regarde ça  pour vous. "}})
    assert approved == "Je regarde ça pour vous."
    assert tool_filler.filler_text(config, approved) == "Je regarde ça pour vous."
    # nothing written by the platform = the runtime's own phrase for the language
    assert tool_filler.approved_filler({"svp_publish": {"version": 3}}) == ""
    assert tool_filler.filler_text({"body_template": {"call": {"language": "de"}}}, "") == tool_filler.FILLERS["de"]


# --- P-52 (ADR-033 amendment of 2026-11-07): a confirmed booking says what it is doing as it
# is sent; the read-back never does; off unless switched on


def test_p52_only_the_confirmed_booking_speaks_and_only_with_the_switch(monkeypatch):
    confirmed = {"confirm": "p1.k1.1762543000.abc.def", "date": "2026-11-11", "time": "16:00"}
    readback = {"date": "2026-11-11", "time": "16:00"}
    monkeypatch.delenv("SVP_WRITE_PROGRESS", raising=False)
    assert tool_filler.write_progress_text("book_appointment", confirmed, _config("fr")) is None
    monkeypatch.setenv("SVP_WRITE_PROGRESS", "on")
    assert (
        tool_filler.write_progress_text("book_appointment", confirmed, _config("fr"))
        == "Un instant, j'enregistre votre rendez-vous."
    )
    assert tool_filler.write_progress_text("book_appointment_2", confirmed, _config("de")) == (
        "Einen Moment, ich trage Ihren Termin ein."
    )
    # the read-back writes nothing: no sentence, nor with an empty confirmation
    assert tool_filler.write_progress_text("book_appointment", readback, _config("fr")) is None
    assert (
        tool_filler.write_progress_text("book_appointment", {**readback, "confirm": " "}, {})
        is None
    )
    # another write, or a read, has no sentence
    assert tool_filler.write_progress_text("create_task", confirmed, _config("fr")) is None
    assert tool_filler.write_progress_text("booking_lookup", confirmed, _config("fr")) is None
    monkeypatch.setenv("SVP_WRITE_PROGRESS", "off")
    assert tool_filler.write_progress_text("book_appointment", confirmed, _config("fr")) is None


@pytest.mark.asyncio
async def test_p52_a_write_that_spoke_waits_with_no_delayed_filler():
    spoken = []

    async def speak(text):
        spoken.append(text)

    async def slow():
        await asyncio.sleep(0.1)
        return "booked"

    assert await tool_filler.with_filler(slow(), speak, "Un instant", after_ms=0) == "booked"
    assert spoken == []
