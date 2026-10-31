"""P-47 (Swiss Voice Platform, D6e, CF-073): what is not for a caller's ears is not spoken."""

import pytest

from api.services.pipecat.digit_groups_filter import svp_text_filters
from api.services.pipecat.spoken_text_guard import SpokenTextGuardFilter, sign_in

NOT_FOR_THE_CALLER = [
    # gpt-oss-120b, the probe of 2026-10-31
    "Il totale è 5 450,00 CHF. <|vq_14776|>#<|start|>assistant<|channel|>commentary",
    "<call:vers_fin/>",
    "Your quote totals €5,450.00. [ends call]",
    "[Calling vers_fin]",
    # Gemini on Vertex (CF-133)
    "call: default_api.lookup_knowledge_afc3909c(query='horaires')",
    # DeepSeek V4 Flash writing an opening itself (CF-234)
    "Let's begin. The caller is on the line.",
    # Kimi K2.6 saying its reasoning (CF-230)
    "L'utilisateur dit « elle pourrait vous laisser un message au secrétaire » - cela semble confus.",
    "The user wants an appointment on Monday.",
    "Der Benutzer möchte einen Termin.",
    "L'utente vuole un appuntamento.",
    "[ALREADY KNOWN] nom: (not given yet)",
]

SPEECH = [
    "Bonjour, ici Driing. Je suis l'assistant virtuel de Driing, une intelligence artificielle.",
    "Votre rendez-vous est confirmé pour mardi 3 novembre à 15 heures.",
    "Le numéro est 0, 7, 9. 1, 2, 3. 4, 5. 6, 7.",
    "Guten Tag, wie kann ich Ihnen helfen?",
    "Buongiorno, come posso aiutarla?",
    "Can I help you with anything else?",
    "Je vous propose lundi à 9 h ou mardi à 14 h 30.",
    "Pour toute question, écrivez à info@driing.ch.",
]


@pytest.mark.parametrize("text", NOT_FOR_THE_CALLER)
def test_p47_what_is_not_for_the_caller_is_recognised(text):
    assert sign_in(text) is not None, text


@pytest.mark.parametrize("text", SPEECH)
def test_p47_a_receptionists_sentences_pass(text):
    assert sign_in(text) is None, text


@pytest.mark.asyncio
async def test_p47_the_filter_drops_the_sentence_whole():
    guard = SpokenTextGuardFilter()
    assert await guard.filter("L'utilisateur dit bonjour.") == ""
    assert await guard.filter("Bonjour, comment puis-je vous aider ?") == (
        "Bonjour, comment puis-je vous aider ?"
    )


def test_p47_switched_on_it_comes_first_unset_it_is_stock(monkeypatch):
    monkeypatch.setenv("SVP_TTS_DIGIT_GROUPS", "digits")
    monkeypatch.delenv("SVP_SPOKEN_TEXT_GUARD", raising=False)
    assert [type(f).__name__ for f in svp_text_filters()] == ["DigitGroupsTextFilter"]
    monkeypatch.setenv("SVP_SPOKEN_TEXT_GUARD", "on")
    assert [type(f).__name__ for f in svp_text_filters()] == [
        "SpokenTextGuardFilter", "DigitGroupsTextFilter",
    ]  # fmt: skip
