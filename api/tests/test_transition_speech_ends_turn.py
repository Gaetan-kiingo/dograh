"""P-27 (ADR-002; CF-131): an edge's transition speech is the next step's first sentence,
spoken by the runtime at the step change, and no model call follows - the model is next
called on the caller's answer and is told what was said. SVP_TRANSITION_SPEECH_ENDS_TURN=1;
unset = stock behaviour (the speech is a filler, the model generates the step's first words)."""

from api.services.workflow.pipecat_engine import transition_result, transition_speech_ends_turn


def test_p27_switch(monkeypatch):
    monkeypatch.delenv("SVP_TRANSITION_SPEECH_ENDS_TURN", raising=False)
    assert transition_speech_ends_turn() is False
    monkeypatch.setenv("SVP_TRANSITION_SPEECH_ENDS_TURN", "1")
    assert transition_speech_ends_turn() is True
    monkeypatch.setenv("SVP_TRANSITION_SPEECH_ENDS_TURN", "yes")
    assert transition_speech_ends_turn() is False


def test_p27_the_result_tells_the_model_what_was_said(monkeypatch):
    monkeypatch.setenv("SVP_TRANSITION_RESULT", "explicit")
    answer = transition_result("Devis", said="Pouvez-vous me donner le numéro de votre devis ?")
    assert answer["status"] == "moved" and answer["step"] == "Devis"
    assert answer["said"] == "Pouvez-vous me donner le numéro de votre devis ?"
    assert "do not repeat it" in answer["note"] and "Nothing else has been done yet" in answer["note"]
    # without speech, P-23's answer is unchanged
    assert "said" not in transition_result("Devis")


def test_p27_stock_result_still_carries_what_was_said(monkeypatch):
    monkeypatch.delenv("SVP_TRANSITION_RESULT", raising=False)
    assert transition_result("Devis") == {"status": "done"}
    assert transition_result("Devis", said="Bonjour") == {"status": "done", "said": "Bonjour"}
