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


# --- P-17: the keys come from the platform once per run and leave memory with the run ---------


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    calls: list[dict] = []
    answer = (200, {"keys": {"SVP_PROVIDER_LLM": {"value": "from-platform", "version": 3}}})

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, json=None, headers=None):
        _FakeClient.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(*_FakeClient.answer)


@pytest.fixture
def key_service(monkeypatch):
    from api.services.configuration import secret_refs

    monkeypatch.setenv("SVP_KEY_SERVICE_URL", "http://platform:8100")
    monkeypatch.setenv("SVP_KEY_SERVICE_TOKEN", "service-bearer")
    monkeypatch.delenv("SVP_PROVIDER_LLM", raising=False)
    monkeypatch.setattr(secret_refs.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.answer = (200, {"keys": {"SVP_PROVIDER_LLM": {"value": "from-platform", "version": 3}}})
    secret_refs._run_keys.clear()
    yield secret_refs
    secret_refs._run_keys.clear()


def _configuration():
    return EffectiveAIModelConfiguration(
        llm=OpenAILLMService(provider="openai", api_key="secretref:SVP_PROVIDER_LLM", model="m")
    )


@pytest.mark.asyncio
async def test_p17_the_platform_is_asked_once_per_run_and_the_keys_stay_for_the_run(key_service):
    stored = _configuration()
    keys = await key_service.keys_for_run(stored, organization_id=5, workflow_id=9, run_id=77)
    assert keys == {"SVP_PROVIDER_LLM": "from-platform"}
    call = _FakeClient.calls[0]
    assert call["url"] == "http://platform:8100/v1/internal/provider-keys"
    assert call["json"] == {"organization_id": 5, "workflow_id": 9, "run_id": 77, "names": ["SVP_PROVIDER_LLM"]}
    assert call["headers"]["Authorization"] == "Bearer service-bearer"
    # a second turn of the same run (a text session) asks nothing
    again = await key_service.keys_for_run(stored, organization_id=5, workflow_id=9, run_id=77)
    assert again == keys and len(_FakeClient.calls) == 1
    live = key_service.resolve_configuration(stored, keys)
    assert live.llm.api_key == "from-platform"
    assert stored.llm.api_key == "secretref:SVP_PROVIDER_LLM"
    assert key_service.held_runs() == 1
    key_service.forget_run(77)
    assert key_service.held_runs() == 0
    # another run asks again
    await key_service.keys_for_run(stored, organization_id=5, workflow_id=9, run_id=78)
    assert len(_FakeClient.calls) == 2


@pytest.mark.asyncio
async def test_p17_a_refusal_or_an_unreachable_platform_fails_the_call_closed(key_service):
    _FakeClient.answer = (403, {"detail": {"code": "key_not_admitted"}})
    with pytest.raises(SecretReferenceError, match="key_not_admitted"):
        await key_service.keys_for_run(_configuration(), organization_id=5, workflow_id=9, run_id=1)
    assert key_service.held_runs() == 0
    _FakeClient.answer = (200, {"keys": {}})
    with pytest.raises(SecretReferenceError, match="did not provide"):
        await key_service.keys_for_run(_configuration(), organization_id=5, workflow_id=9, run_id=2)


@pytest.mark.asyncio
async def test_p17_without_the_key_service_the_environment_answers_as_before(monkeypatch):
    from api.services.configuration import secret_refs

    monkeypatch.delenv("SVP_KEY_SERVICE_URL", raising=False)
    monkeypatch.setenv("SVP_PROVIDER_LLM", "from-env")
    assert await secret_refs.keys_for_run(_configuration(), organization_id=1, workflow_id=1, run_id=1) is None
    assert secret_refs.resolve_configuration(_configuration(), None).llm.api_key == "from-env"


def test_p17_the_run_map_is_bounded(key_service):
    import time

    for run_id in range(key_service._RUN_KEYS_MAX + 10):
        key_service._run_keys[run_id] = ({"SVP_PROVIDER_LLM": "x"}, time.monotonic())
    key_service._sweep(time.monotonic())
    assert key_service.held_runs() == key_service._RUN_KEYS_MAX
