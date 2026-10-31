"""P-46 (Swiss Voice Platform, ADR-065): the in-call fallback's configuration path.

The platform compiles the next engine of the approved chain into the workflow's top-level
``svp_llm_fallback`` (never inside ``model_overrides``: the save turns those into the
organisation's v2 override and drops unknown sections); with ``SVP_LLM_FALLBACK_AFTER_MS`` set
the runtime keeps it beside ``llm``, fetches its key for the run like the others (P-17) and
hands it to the OpenAI-compatible service with its own P-22 parameters. Unset = stock."""

import json

import pytest

from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.services.configuration import secret_refs
from api.services.configuration.registry import OpenAILLMService
from api.services.configuration import ai_model_configuration
from api.services.configuration.resolve import svp_llm_fallback_section
from api.services.pipecat import service_factory

PRIMARY = {"provider": "openai", "model": "inference-deepseek-v4-flash",
           "base_url": "https://maas.phoeniqs.com/v1",
           "api_key": "secretref:SVP_PROVIDER_PHOENIQS_LLM_API_KEY"}  # fmt: skip
FALLBACK = {"provider": "openai", "model": "moonshotai/Kimi-K2.6",
            "base_url": "https://api.infomaniak.com/2/ai/111571/openai/v1",
            "api_key": "secretref:SVP_PROVIDER_INFOMANIAK_API_KEY"}  # fmt: skip


def _global():
    return EffectiveAIModelConfiguration(llm=OpenAILLMService(**PRIMARY))


def _with_fallback(block=FALLBACK):
    effective = _global()
    section = svp_llm_fallback_section({"svp_llm_fallback": block, "model_overrides": {}})
    return effective.model_copy(update={"llm_fallback": section}) if section else effective


def test_p46_the_fallback_is_read_from_the_top_level_key_only_when_switched_on(monkeypatch):
    monkeypatch.delenv("SVP_LLM_FALLBACK_AFTER_MS", raising=False)
    assert svp_llm_fallback_section({"svp_llm_fallback": FALLBACK}) is None
    monkeypatch.setenv("SVP_LLM_FALLBACK_AFTER_MS", "1500")
    assert svp_llm_fallback_section({"svp_llm_fallback": FALLBACK}).model == "moonshotai/Kimi-K2.6"
    # never from inside model_overrides (the v2 conversion would have dropped it anyway)
    assert svp_llm_fallback_section({"model_overrides": {"llm_fallback": FALLBACK}}) is None


@pytest.mark.asyncio
async def test_p46_the_run_configuration_carries_it_on_the_v2_path_too(monkeypatch):
    """The organisation's v2 override path (found on the live proof of 2026-10-31: the save
    had turned model_overrides into the v2 override and the fallback was gone)."""
    monkeypatch.setenv("SVP_LLM_FALLBACK_AFTER_MS", "1500")
    monkeypatch.setattr(ai_model_configuration, "compile_ai_model_configuration_v2", lambda _: _global())
    monkeypatch.setattr(
        ai_model_configuration.OrganizationAIModelConfigurationV2, "model_validate", lambda v: v
    )

    async def no_keys(*_, **__):
        return None

    monkeypatch.setattr(secret_refs, "keys_for_run", no_keys)
    monkeypatch.setenv("SVP_PROVIDER_PHOENIQS_LLM_API_KEY", "p")
    monkeypatch.setenv("SVP_PROVIDER_INFOMANIAK_API_KEY", "i")
    effective = await ai_model_configuration.get_effective_ai_model_configuration_for_workflow(
        organization_id=1,
        workflow_configurations={"model_configuration_v2_override": {"x": 1},
                                 "svp_llm_fallback": FALLBACK},
    )  # fmt: skip
    assert effective.llm_fallback.model == "moonshotai/Kimi-K2.6"
    assert effective.llm_fallback.api_key == "i"  # resolved in memory for the run


def test_p46_the_fallbacks_key_is_asked_for_the_run_and_resolved(monkeypatch):
    monkeypatch.setenv("SVP_LLM_FALLBACK_AFTER_MS", "1500")
    effective = _with_fallback()
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
    resolved = secret_refs.resolve_configuration(
        _with_fallback(),
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
    assert service_factory._svp_llm_fallback(_with_fallback(PRIMARY)) is None
    with_fallback = _with_fallback()
    monkeypatch.delenv("SVP_LLM_FALLBACK_AFTER_MS")
    assert service_factory._svp_llm_fallback(with_fallback) is None
