"""P-22 (ADR-002; plan package C9, ADR-029 amendment of 2026-09-29): extra request
parameters per model from SVP_LLM_EXTRA_BODY - Kimi K2.6 reasoned before every answer
(an 11.5 s first word, 38 s extractions); its requests now carry « thinking: off ».
Per model: Mistral 3, the fallback, answers HTTP 400 to a parameter it does not know.
Unset = stock behaviour."""

import json

from api.services.pipecat import service_factory

KIMI = "moonshotai/Kimi-K2.6"
OFF = {KIMI: {"chat_template_kwargs": {"thinking": False}}}


def _llm(model: str):
    return service_factory.create_llm_service_from_provider(
        "openai", model, "sk-test", base_url="https://api.infomaniak.com/2/ai/1/openai/v1"
    )


def _body(llm) -> dict:
    return llm.build_chat_completion_params({"messages": []}).get("extra_body") or {}


def test_p22_the_named_model_carries_its_parameters_in_every_request(monkeypatch):
    monkeypatch.setenv("SVP_LLM_EXTRA_BODY", json.dumps(OFF))
    assert _body(_llm(KIMI)) == {"chat_template_kwargs": {"thinking": False}}


def test_p22_another_model_carries_nothing(monkeypatch):
    monkeypatch.setenv("SVP_LLM_EXTRA_BODY", json.dumps(OFF))
    assert _body(_llm("mistral3")) == {}


def test_p22_unset_or_malformed_is_stock_behaviour(monkeypatch):
    monkeypatch.delenv("SVP_LLM_EXTRA_BODY", raising=False)
    assert _body(_llm(KIMI)) == {}
    for raw in ("not json", '{"moonshotai/Kimi-K2.6": "off"}', "[1, 2]"):
        monkeypatch.setenv("SVP_LLM_EXTRA_BODY", raw)
        assert _body(_llm(KIMI)) == {}, raw
