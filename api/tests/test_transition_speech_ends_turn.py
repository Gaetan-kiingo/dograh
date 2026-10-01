"""P-27 (ADR-002; CF-131): an edge's transition speech is the next step's first sentence,
spoken by the runtime at the step change; the model is told what was said. SVP_TRANSITION_SPEECH=bridge
(the model is still called and adds words only when the turn needs more - a referral, « I don't
know ») or ends_turn (no model call follows); unset = stock behaviour."""

from api.services.workflow.pipecat_engine import transition_result, transition_speech_mode


def test_p27_switch(monkeypatch):
    monkeypatch.delenv("SVP_TRANSITION_SPEECH", raising=False)
    assert transition_speech_mode() == ""
    monkeypatch.setenv("SVP_TRANSITION_SPEECH", "bridge")
    assert transition_speech_mode() == "bridge"
    monkeypatch.setenv("SVP_TRANSITION_SPEECH", "ends_turn")
    assert transition_speech_mode() == "ends_turn"
    monkeypatch.setenv("SVP_TRANSITION_SPEECH", "1")
    assert transition_speech_mode() == ""  # stock: an unknown value switches nothing on


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
