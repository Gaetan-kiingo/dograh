"""P-23 (ADR-002; plan package C8a, CF-087): a step change says what it is. Stock
Dograh answers {"status": "done"}; beside a link named « Rappel » the model read the recall
request as done and claimed it to the caller. SVP_TRANSITION_RESULT=explicit answers that
the call moved and that nothing has been done. Unset = stock behaviour."""

from api.services.workflow.pipecat_engine import transition_result


def test_p23_explicit_says_the_call_moved_and_nothing_is_done(monkeypatch):
    monkeypatch.setenv("SVP_TRANSITION_RESULT", "explicit")
    answer = transition_result("Demande de rappel")
    assert answer["status"] == "moved" and answer["step"] == "Demande de rappel"
    assert "Nothing has been done yet" in answer["note"]
    assert "done" not in answer["status"]


def test_p23_unset_is_stock_behaviour(monkeypatch):
    monkeypatch.delenv("SVP_TRANSITION_RESULT", raising=False)
    assert transition_result("Demande de rappel") == {"status": "done"}
    monkeypatch.setenv("SVP_TRANSITION_RESULT", "something else")
    assert transition_result("x") == {"status": "done"}
