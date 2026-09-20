"""P-13 (Swiss Voice Platform): provider-key references."""

import pytest

from api.services.configuration.registry import OpenAILLMService
from api.services.configuration.secret_refs import (
    SecretReferenceError,
    resolve_configuration,
    resolve_value,
)
from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration


def test_a_literal_key_is_untouched():
    assert resolve_value("sk-literal") == "sk-literal"
    assert resolve_value(None) is None


def test_a_reference_becomes_the_environment_value(monkeypatch):
    monkeypatch.setenv("SVP_PROVIDER_X_API_KEY", "real")
    assert resolve_value("secretref:SVP_PROVIDER_X_API_KEY") == "real"
    assert resolve_value(["secretref:SVP_PROVIDER_X_API_KEY", "lit"]) == ["real", "lit"]


@pytest.mark.parametrize(
    "reference",
    ["secretref:DATABASE_URL", "secretref:OSS_JWT_SECRET", "secretref:PATH", "secretref:SVP_PROVIDER_", "secretref:svp_provider_x"],
)
def test_only_provider_names_are_resolvable(reference, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    with pytest.raises(SecretReferenceError, match="not allowed"):
        resolve_value(reference)


def test_an_unset_reference_is_an_error_not_a_literal(monkeypatch):
    monkeypatch.delenv("SVP_PROVIDER_MISSING", raising=False)
    with pytest.raises(SecretReferenceError, match="not set"):
        resolve_value("secretref:SVP_PROVIDER_MISSING")


def test_a_configuration_is_resolved_on_a_copy(monkeypatch):
    monkeypatch.setenv("SVP_PROVIDER_LLM", "real-llm-key")
    stored = EffectiveAIModelConfiguration(
        llm=OpenAILLMService(provider="openai", api_key="secretref:SVP_PROVIDER_LLM", model="m")
    )
    live = resolve_configuration(stored)
    assert live.llm.api_key == "real-llm-key"
    assert stored.llm.api_key == "secretref:SVP_PROVIDER_LLM"  # what gets saved or shown keeps the reference
