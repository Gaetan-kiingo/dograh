"""Swiss Voice Platform, C11 (ADR-045 decision 20): Google's engines through the runtime.

- P-13 amendment: a `credentials` reference (a service account's JSON) resolves like a key;
- P-22 amendment: the Vertex LLM service takes Gemini's thinking budget from SVP_LLM_EXTRA_BODY
  and P-19's temperature.
"""

import json

import pytest

from api.services.configuration import secret_refs


def test_a_credentials_reference_resolves_from_the_environment(monkeypatch):
    monkeypatch.setenv("SVP_PROVIDER_GOOGLE_CREDENTIALS", '{"type": "service_account"}')
    assert "credentials" in secret_refs._SECRET_FIELDS
    value = secret_refs.resolve_value("secretref:SVP_PROVIDER_GOOGLE_CREDENTIALS")
    assert json.loads(value)["type"] == "service_account"


def test_the_vertex_service_gets_the_thinking_budget_and_the_temperature(monkeypatch):
    from api.services.pipecat import service_factory as sf

    monkeypatch.setenv("SVP_LLM_EXTRA_BODY", json.dumps({"gemini-2.5-flash-lite": {"thinking_budget": 0}}))
    monkeypatch.setenv("SVP_LLM_TEMPERATURE", "0.25")
    captured = {}

    class FakeVertex:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(sf, "DograhGoogleVertexLLMService", FakeVertex)
    sf.create_llm_service_from_provider(
        "google_vertex",
        "gemini-2.5-flash-lite",
        None,
        credentials='{"type": "service_account"}',
        project_id="driing-eu",
        location="europe-west1",
    )
    settings = captured["settings"]
    assert settings.model == "gemini-2.5-flash-lite"
    assert settings.temperature == 0.25
    assert settings.thinking.thinking_budget == 0
    assert captured["location"] == "europe-west1"


def test_without_an_extra_body_the_vertex_service_is_stock(monkeypatch):
    from api.services.pipecat import service_factory as sf

    monkeypatch.delenv("SVP_LLM_EXTRA_BODY", raising=False)
    monkeypatch.delenv("SVP_LLM_TEMPERATURE", raising=False)
    captured = {}

    class FakeVertex:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(sf, "DograhGoogleVertexLLMService", FakeVertex)
    sf.create_llm_service_from_provider(
        "google_vertex", "gemini-2.5-flash", None, credentials="{}", project_id="p", location="europe-west1"
    )
    from pipecat.services.google.llm import GoogleLLMService

    assert not isinstance(captured["settings"].thinking, GoogleLLMService.ThinkingConfig)
    assert captured["settings"].temperature == sf.STOCK_LLM_TEMPERATURE
