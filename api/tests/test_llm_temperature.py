"""P-19 (ADR-002): the call LLM's sampling temperature comes from SVP_LLM_TEMPERATURE;
unset = the stock 0.1, a bad value is refused loudly and the stock value used."""

import pytest

from api.services.pipecat import service_factory as sf


def test_unset_is_the_stock_temperature(monkeypatch):
    monkeypatch.delenv("SVP_LLM_TEMPERATURE", raising=False)
    assert sf._llm_temperature() == sf.STOCK_LLM_TEMPERATURE == 0.1
    monkeypatch.setenv("SVP_LLM_TEMPERATURE", "   ")
    assert sf._llm_temperature() == 0.1


@pytest.mark.parametrize("raw,expected", [("0.25", 0.25), ("0", 0.0), ("2", 2.0), ("0.7", 0.7)])
def test_a_value_in_range_is_used(monkeypatch, raw, expected):
    monkeypatch.setenv("SVP_LLM_TEMPERATURE", raw)
    assert sf._llm_temperature() == expected


@pytest.mark.parametrize("raw", ["hot", "-0.1", "2.5", "1e9"])
def test_a_bad_value_falls_back_to_stock(monkeypatch, raw):
    monkeypatch.setenv("SVP_LLM_TEMPERATURE", raw)
    assert sf._llm_temperature() == 0.1


def test_the_openai_service_is_built_with_it(monkeypatch):
    monkeypatch.setenv("SVP_LLM_TEMPERATURE", "0.25")
    monkeypatch.setenv("SVP_KEY_SERVICE_URL", "")
    captured = {}

    class Settings:
        def __init__(self, model, temperature=None, extra=None):
            captured["model"], captured["temperature"] = model, temperature

    class Service:
        def __init__(self, api_key, settings, **kwargs):
            captured["built"] = True

    monkeypatch.setattr(sf, "OpenAILLMSettings", Settings)
    monkeypatch.setattr(sf, "OpenAILLMService", Service)
    sf.create_llm_service_from_provider(
        provider=sf.ServiceProviders.OPENAI.value, model="moonshotai/Kimi-K2.6", api_key="k"
    )
    assert captured == {"model": "moonshotai/Kimi-K2.6", "temperature": 0.25, "built": True}
