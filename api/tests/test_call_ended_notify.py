"""P-20 (ADR-002, ADR-037 point 8): the platform is told when a call ended - after the
run's artifacts and integrations - with the runtime's bearer; unset = nothing is sent,
and a failure never breaks the completion."""

from types import SimpleNamespace

import httpx
import pytest

from api.services import platform_notify
from api.tasks import workflow_completion


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    calls: list[dict] = []
    status = 202
    fail = False

    def __init__(self, *a, **k):
        _FakeClient.timeout = k.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, json=None, headers=None):
        if _FakeClient.fail:
            raise httpx.ConnectError("platform down")
        _FakeClient.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(_FakeClient.status)


class _Db:
    async def get_workflow_run_with_context(self, run_id):
        return SimpleNamespace(workflow_id=9), 5


@pytest.fixture
def notify(monkeypatch):
    import api.db as db_module

    monkeypatch.setattr(platform_notify.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(db_module, "db_client", _Db())
    _FakeClient.calls, _FakeClient.status, _FakeClient.fail = [], 202, False
    monkeypatch.setenv("SVP_KEY_SERVICE_TOKEN", "service-bearer")
    yield platform_notify


@pytest.mark.asyncio
async def test_p20_unset_sends_nothing(notify, monkeypatch):
    monkeypatch.delenv("SVP_CALL_ENDED_URL", raising=False)
    assert await notify.notify_call_ended(77) is False
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_p20_set_tells_the_platform_with_the_runtimes_bearer(notify, monkeypatch):
    monkeypatch.setenv("SVP_CALL_ENDED_URL", "http://platform:8100/v1/internal/calls/ended")
    assert await notify.notify_call_ended(77) is True
    (call,) = _FakeClient.calls
    assert call["url"] == "http://platform:8100/v1/internal/calls/ended"
    assert call["json"] == {"organization_id": 5, "workflow_id": 9, "run_id": 77}
    assert call["headers"] == {"Authorization": "Bearer service-bearer"}
    assert _FakeClient.timeout == notify.CALL_ENDED_TIMEOUT


@pytest.mark.asyncio
async def test_p20_a_platform_that_refuses_or_is_down_never_raises(notify, monkeypatch):
    monkeypatch.setenv("SVP_CALL_ENDED_URL", "http://platform:8100/v1/internal/calls/ended")
    _FakeClient.status = 401
    assert await notify.notify_call_ended(77) is False
    _FakeClient.fail = True
    assert await notify.notify_call_ended(77) is False
    monkeypatch.delenv("SVP_KEY_SERVICE_TOKEN")
    assert await notify.notify_call_ended(77) is False


@pytest.mark.asyncio
async def test_p20_the_completion_tells_the_platform_after_the_integrations(monkeypatch):
    order: list[str] = []

    async def integrations(_ctx, run_id):
        order.append("integrations")

    async def told(run_id):
        order.append("platform")
        raise RuntimeError("even a bug here does not stop the completion")

    async def billing(run_id):
        order.append("billing")

    monkeypatch.setattr(workflow_completion, "run_integrations_post_workflow_run", integrations)
    monkeypatch.setattr(workflow_completion, "notify_call_ended", told)
    monkeypatch.setattr(
        workflow_completion, "report_completed_workflow_run_platform_usage", billing
    )
    await workflow_completion.process_workflow_completion({}, 77)
    assert order == ["integrations", "platform", "billing"]
