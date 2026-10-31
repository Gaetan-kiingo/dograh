"""P-46 (Swiss Voice Platform, ADR-065): the in-call fallback's configuration path.

The platform compiles the next engine of the approved chain into the workflow's
``model_overrides.llm_fallback``; with ``SVP_LLM_FALLBACK_AFTER_MS`` set the runtime keeps it
beside ``llm``, fetches its key for the run like the others (P-17) and hands it to the
OpenAI-compatible service with its own P-22 parameters. Unset = stock."""

import json

from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.services.configuration import secret_refs
from api.services.configuration.registry import OpenAILLMService
from api.services.configuration.resolve import resolve_effective_config
from api.services.pipecat import service_factory

PRIMARY = {"provider": "openai", "model": "inference-deepseek-v4-flash",
           "base_url": "https://maas.phoeniqs.com/v1",
           "api_key": "secretref:SVP_PROVIDER_PHOENIQS_LLM_API_KEY"}  # fmt: skip
FALLBACK = {"provider": "openai", "model": "moonshotai/Kimi-K2.6",
            "base_url": "https://api.infomaniak.com/2/ai/111571/openai/v1",
            "api_key": "secretref:SVP_PROVIDER_INFOMANIAK_API_KEY"}  # fmt: skip


def _global():
    return EffectiveAIModelConfiguration(llm=OpenAILLMService(**PRIMARY))


def test_p46_the_fallback_is_kept_beside_the_llm_only_when_switched_on(monkeypatch):
    overrides = {"llm": PRIMARY, "llm_fallback": FALLBACK}
    monkeypatch.delenv("SVP_LLM_FALLBACK_AFTER_MS", raising=False)
    assert resolve_effective_config(_global(), overrides).llm_fallback is None
    monkeypatch.setenv("SVP_LLM_FALLBACK_AFTER_MS", "1500")
    effective = resolve_effective_config(_global(), overrides)
    assert effective.llm_fallback.model == "moonshotai/Kimi-K2.6"
    assert effective.llm.model == "inference-deepseek-v4-flash"


def test_p46_the_fallbacks_key_is_asked_for_the_run_and_resolved(monkeypatch):
    monkeypatch.setenv("SVP_LLM_FALLBACK_AFTER_MS", "1500")
    effective = resolve_effective_config(_global(), {"llm_fallback": FALLBACK})
    assert secret_refs._names_in(effective) == [
        "SVP_PROVIDER_PHOENIQS_LLM_API_KEY",
        "SVP_PROVIDER_INFOMANIAK_API_KEY",
    ]
    keys = {"SVP_PROVIDER_PHOENIQS_LLM_API_KEY": "p", "SVP_PROVIDER_INFOMANIAK_API_KEY": "i"}
    resolved = secret_refs.resolve_configuration(effective, keys)
    assert resolved.llm.api_key == "p" and resolved.llm_fallback.api_key == "i"
    assert effective.llm_fallback.api_key.startswith("secretref:")  # the stored copy untouched


def test_p46_the_service_gets_the_fallback_with_its_own_parameters(monkeypatch):
    monkeypatch.setenv("SVP_LLM_FALLBACK_AFTER_MS", "1500")
    monkeypatch.setenv("SVP_LLM_EXTRA_BODY", json.dumps({
        "inference-deepseek-v4-flash": {"chat_template_kwargs": {"thinking": False}},
        "moonshotai/Kimi-K2.6": {"chat_template_kwargs": {"thinking": False}, "k": 1},
    }))  # fmt: skip
    effective = resolve_effective_config(_global(), {"llm_fallback": FALLBACK})
    resolved = secret_refs.resolve_configuration(
        effective,
        {"SVP_PROVIDER_PHOENIQS_LLM_API_KEY": "p", "SVP_PROVIDER_INFOMANIAK_API_KEY": "i"},
    )
    fallback = service_factory._svp_llm_fallback(resolved)
    assert fallback == {
        "base_url": FALLBACK["base_url"], "api_key": "i", "model": "moonshotai/Kimi-K2.6",
        "extra_body": {"chat_template_kwargs": {"thinking": False}, "k": 1}, "after_secs": 1.5,
    }  # fmt: skip
    service = service_factory.create_llm_service(resolved)
    assert service._svp_fallback["model"] == "moonshotai/Kimi-K2.6"


def test_p46_switched_off_or_the_same_engine_means_no_fallback(monkeypatch):
    monkeypatch.setenv("SVP_LLM_FALLBACK_AFTER_MS", "1500")
    same = resolve_effective_config(_global(), {"llm_fallback": PRIMARY})
    assert service_factory._svp_llm_fallback(same) is None
    monkeypatch.delenv("SVP_LLM_FALLBACK_AFTER_MS")
    assert service_factory._svp_llm_fallback(
        resolve_effective_config(_global(), {"llm_fallback": FALLBACK})
    ) is None
