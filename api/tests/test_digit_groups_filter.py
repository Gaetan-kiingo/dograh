"""P-21 (ADR-002): digit groups joined by dashes are said as groups. Azure's fr-CH voice
reads « 1-5-2 » as « un moins cinq moins deux », its de-CH voice as « eins bis fünf bis
zwei » (2026-09-25, the owner's call 675); SVP_TTS_DIGIT_GROUPS=comma turns commas in;
unset = stock behaviour."""

import asyncio

import pytest

from api.services.pipecat import digit_groups_filter as dg


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Le numéro est 1-5-2.", "Le numéro est 1, 5, 2."),
        ("Le numéro est 1-5-2-1-5-2.", "Le numéro est 1, 5, 2, 1, 5, 2."),
        ("Le devis 2026-02-132 est ouvert.", "Le devis 2026, 02, 132 est ouvert."),
        ("La facture 65 - 2026 - 02 - 133.", "La facture 65, 2026, 02, 133."),
        ("Die Nummer ist 1‑5‑2.", "Die Nummer ist 1, 5, 2."),
    ],
)
def test_a_chain_of_three_groups_or_more_is_said_as_groups(text, expected):
    assert dg.said_as_groups(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Ouvert de 9-12 heures.",  # two groups may be a range: left to the voice
        "Le numéro est 152.",
        "Un rendez-vous mardi à 14 h.",
        "Le 079 555 12 34.",
        "Le devis RE-2026 est ouvert.",
    ],
)
def test_everything_else_is_left_as_written(text):
    assert dg.said_as_groups(text) == text


def test_unset_is_stock_behaviour(monkeypatch):
    monkeypatch.delenv("SVP_TTS_DIGIT_GROUPS", raising=False)
    assert dg.svp_text_filters() == []
    monkeypatch.setenv("SVP_TTS_DIGIT_GROUPS", "off")
    assert dg.svp_text_filters() == []


def test_set_the_filter_runs_before_speech(monkeypatch):
    monkeypatch.setenv("SVP_TTS_DIGIT_GROUPS", "comma")
    (only,) = dg.svp_text_filters()
    assert asyncio.run(only.filter("Le numéro est 1-5-2.")) == "Le numéro est 1, 5, 2."


def test_the_azure_voice_is_built_with_it(monkeypatch):
    from types import SimpleNamespace

    from api.services.pipecat import service_factory as sf

    monkeypatch.setenv("SVP_TTS_DIGIT_GROUPS", "comma")
    captured = {}

    class Service:
        def __init__(self, text_filters, **kwargs):
            captured["filters"] = [type(f).__name__ for f in text_filters]

    monkeypatch.setattr(sf, "AzureTTSService", Service)
    tts = SimpleNamespace(provider=sf.ServiceProviders.AZURE_SPEECH.value, model="neural",
                          api_key="k", region="switzerlandnorth", voice="fr-CH-ArianeNeural",
                          language="fr-CH", speed=1.0)  # fmt: skip
    sf.create_tts_service(SimpleNamespace(tts=tts), audio_config=None)
    assert captured["filters"] == ["XMLFunctionTagFilter", "DigitGroupsTextFilter"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Le devis 2026-02-147 est ouvert.", "Le devis 2, 0, 2, 6. 0, 2. 1, 4, 7 est ouvert."),
        ("Le numéro est 1-5-2.", "Le numéro est 1, 5, 2."),
        ("La facture 65 - 2026 - 02 - 133.", "La facture 6, 5. 2, 0, 2, 6. 0, 2. 1, 3, 3."),
    ],
)
def test_digits_mode_says_a_reference_digit_by_digit(text, expected):
    # the owner's rule of 2026-09-25 (call 726: « 2026, 02, 135 » came out in thousands)
    assert dg.said_digit_by_digit(text) == expected


@pytest.mark.parametrize("text", ["Ouvert de 9-12 heures.", "Le montant est 1745.80 francs.", "Le 152."])
def test_digits_mode_leaves_everything_else(text):
    assert dg.said_digit_by_digit(text) == text


def test_digits_mode_is_chosen_by_the_setting(monkeypatch):
    monkeypatch.setenv("SVP_TTS_DIGIT_GROUPS", "digits")
    (only,) = dg.svp_text_filters()
    assert asyncio.run(only.filter("Le devis 2026-02-147.")) == "Le devis 2, 0, 2, 6. 0, 2. 1, 4, 7."
