"""P-24 (ADR-002; the owner's request of 2026-09-29, CF-117): an archived agent takes no
call. Every run start - telephony, browser, text chat, campaign, embed - goes through
authorize_workflow_run_start; with SVP_REFUSE_ARCHIVED_WORKFLOWS=1 it refuses a workflow
whose status is archived. Unset = stock behaviour."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services import quota_service


def _patch(monkeypatch, status: str):
    workflow = SimpleNamespace(
        id=7, user_id=123, organization_id=42, status=status,
        workflow_configurations={"model_overrides": {}},
    )
    monkeypatch.setattr(quota_service.db_client, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(
        quota_service.db_client, "get_user_by_id",
        AsyncMock(return_value=SimpleNamespace(id=123, provider_id="p")),
    )
    monkeypatch.setattr(
        quota_service, "get_effective_ai_model_configuration_for_workflow",
        AsyncMock(return_value=SimpleNamespace(
            managed_service_version=2, llm=SimpleNamespace(provider="openai", api_key="k"),
            stt=None, tts=None, embeddings=None,
        )),
    )
    monkeypatch.setattr(quota_service, "DEPLOYMENT_MODE", "oss")


@pytest.mark.asyncio
async def test_p24_an_archived_workflow_takes_no_call(monkeypatch):
    monkeypatch.setenv("SVP_REFUSE_ARCHIVED_WORKFLOWS", "1")
    _patch(monkeypatch, "archived")
    result = await quota_service.authorize_workflow_run_start(workflow_id=7, organization_id=42)
    assert result.has_quota is False and result.error_code == "workflow_archived"


@pytest.mark.asyncio
async def test_p24_an_active_workflow_is_unchanged(monkeypatch):
    monkeypatch.setenv("SVP_REFUSE_ARCHIVED_WORKFLOWS", "1")
    _patch(monkeypatch, "active")
    result = await quota_service.authorize_workflow_run_start(workflow_id=7, organization_id=42)
    assert result.has_quota is True


@pytest.mark.asyncio
async def test_p24_unset_is_stock_behaviour(monkeypatch):
    monkeypatch.delenv("SVP_REFUSE_ARCHIVED_WORKFLOWS", raising=False)
    _patch(monkeypatch, "archived")
    result = await quota_service.authorize_workflow_run_start(workflow_id=7, organization_id=42)
    assert result.has_quota is True
