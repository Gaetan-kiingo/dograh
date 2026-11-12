"""F5c (Swiss Voice Platform, ADR-071 amendment of 2026-11-06; ADR-002 P-51; OPS-001,
SEC-013, CF-249, CF-209): a blocked caller is refused before the call is answered - the ARI
path hangs the unanswered channel up with cause 21, the Twilio path answers <Reject> - and a
screening the platform does not answer lets the call through (fail open). Every inbound call
carries how far its caller-ID can be trusted: network, presented, withheld."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from loguru import logger

from api.services import svp_call_events as events
from api.services import svp_caller_screen as screen_mod
from api.services.call_concurrency import CallConcurrencyLimitError
from api.services.telephony import ari_manager

URL = "http://platform.test/v1/internal/calls/screen"
CALLER = "+41791234567"


@pytest.fixture(autouse=True)
def _plan_limits_off(monkeypatch):
    """These tests assert the stock concurrency refusal; P-55's busy answer is its own test."""
    monkeypatch.delenv("SVP_PLAN_LIMITS", raising=False)


@pytest.fixture
def switched_on(monkeypatch):
    monkeypatch.setenv("SVP_CALLER_SCREEN_URL", URL)
    monkeypatch.setenv("SVP_KEY_SERVICE_TOKEN", "bearer-of-the-runtime")
    monkeypatch.delenv("SVP_CALLER_SCREEN_TIMEOUT_MS", raising=False)


def _platform(monkeypatch, answer, seen: list):
    """The platform's answer through a mock transport; what was asked kept in `seen`."""
    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return (
            httpx.Response(status, json=body)
            if isinstance(body, dict)
            else httpx.Response(status, text=body)
        )

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def _logged():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    return lines, sink


# --- the switch ----------------------------------------------------------------------------------


async def test_p51_nothing_is_asked_without_the_switch(monkeypatch):
    monkeypatch.delenv("SVP_CALLER_SCREEN_URL", raising=False)
    seen: list = []
    _platform(monkeypatch, (200, {"action": "reject", "list": "platform"}), seen)
    assert screen_mod.enabled() is False
    assert (
        await screen_mod.screen(
            organization_id=7,
            caller_number=CALLER,
            called_number="+41445550011",
            provider="ari",
            caller_trust="presented",
        )
        is False
    )
    assert seen == []


# --- OPS-001: the platform's answer --------------------------------------------------------------


async def test_ops001_a_caller_on_a_list_is_refused_and_the_request_carries_the_facts(
    monkeypatch, switched_on
):
    seen: list = []
    _platform(monkeypatch, (200, {"action": "reject", "list": "workspace"}), seen)
    lines, sink = _logged()
    try:
        refused = await screen_mod.screen(
            organization_id=7,
            caller_number=CALLER,
            called_number="+41445550011",
            provider="ari",
            caller_trust="network",
        )
    finally:
        logger.remove(sink)
    assert refused is True
    request = seen[0]
    assert str(request.url) == URL
    assert request.headers["authorization"] == "Bearer bearer-of-the-runtime"
    body = __import__("json").loads(request.content)
    assert body == {
        "organization_id": 7,
        "caller_number": CALLER,
        "called_number": "+41445550011",
        "provider": "ari",
        "caller_trust": "network",
    }
    assert any(
        "blocked caller refused for org 7, list workspace" in line for line in lines
    )
    assert not any(
        CALLER in line or "791234567" in line for line in lines
    )  # never the number


async def test_ops001_a_caller_on_no_list_goes_through(monkeypatch, switched_on):
    seen: list = []
    _platform(monkeypatch, (200, {"action": "allow", "list": None}), seen)
    assert (
        await screen_mod.screen(
            organization_id=7,
            caller_number=CALLER,
            called_number="+41445550011",
            provider="twilio",
            caller_trust="presented",
        )
        is False
    )
    assert len(seen) == 1


async def test_p51_a_withheld_number_is_asked_without_a_number(
    monkeypatch, switched_on
):
    seen: list = []
    _platform(monkeypatch, (200, {"action": "allow"}), seen)
    await screen_mod.screen(
        organization_id=7,
        caller_number="anonymous",
        called_number="+41445550011",
        provider="ari",
        caller_trust="withheld",
    )
    body = __import__("json").loads(seen[0].content)
    assert body["caller_number"] is None and body["caller_trust"] == "withheld"


# --- fail open -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        (503, "unavailable"),
        (401, {"detail": {"code": "service_auth_required"}}),
        (200, "not json"),
        (200, {"verdict": "reject"}),
        httpx.ConnectError("refused"),
    ],
)
async def test_p51_any_failure_lets_the_call_through_and_says_so_without_the_number(
    monkeypatch, switched_on, answer
):
    seen: list = []
    _platform(monkeypatch, answer, seen)
    lines, sink = _logged()
    try:
        refused = await screen_mod.screen(
            organization_id=7,
            caller_number=CALLER,
            called_number="+41445550011",
            provider="ari",
            caller_trust="presented",
        )
    finally:
        logger.remove(sink)
    assert refused is False
    assert any("call let through" in line for line in lines)
    assert not any("791234567" in line for line in lines)


async def test_p51_a_platform_that_never_answers_is_waited_for_the_timeout_only(
    monkeypatch, switched_on
):
    """A server that accepts and never answers: the call goes through after the timeout."""

    async def silent(reader, writer):
        await asyncio.sleep(30)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv(
        "SVP_CALLER_SCREEN_URL", f"http://127.0.0.1:{port}/v1/internal/calls/screen"
    )
    monkeypatch.setenv("SVP_CALLER_SCREEN_TIMEOUT_MS", "200")
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        refused = await asyncio.wait_for(
            screen_mod.screen(
                organization_id=7,
                caller_number=CALLER,
                called_number="+41445550011",
                provider="ari",
                caller_trust="presented",
            ),
            timeout=5,
        )
        waited = loop.time() - started
    finally:
        server.close()
    assert refused is False
    assert waited < 2


def test_p51_the_timeout_is_bounded(monkeypatch):
    monkeypatch.setenv("SVP_CALLER_SCREEN_TIMEOUT_MS", "999999")
    assert screen_mod.timeout_seconds() == 5.0
    monkeypatch.setenv("SVP_CALLER_SCREEN_TIMEOUT_MS", "nonsense")
    assert screen_mod.timeout_seconds() == 0.8


# --- SEC-013: the caller-ID's trust --------------------------------------------------------------


def test_sec013_the_edges_word_is_kept_and_the_runtime_classes_the_rest():
    assert screen_mod.trust_of(CALLER, "network") == "network"
    assert screen_mod.trust_of(CALLER, " Withheld ") == "withheld"
    assert screen_mod.trust_of(CALLER, "") == "presented"
    assert screen_mod.trust_of(CALLER, "made-up") == "presented"
    assert screen_mod.trust_of("unknown", None) == "withheld"  # the ARI event's default
    assert screen_mod.trust_of("", None) == "withheld"
    assert screen_mod.trust_of("anonymous", None) == "withheld"


@pytest.mark.parametrize(
    "form, trust",
    [
        ({"From": CALLER}, "presented"),
        ({"From": CALLER, "StirVerstat": "TN-Validation-Passed-A"}, "network"),
        ({"From": CALLER, "StirVerstat": "TN-Validation-Passed-B"}, "presented"),
        ({"From": "+266696687"}, "withheld"),
        ({"From": "anonymous"}, "withheld"),
        ({"From": "restricted"}, "withheld"),
        ({"From": ""}, "withheld"),
        ({}, "withheld"),
    ],
)
def test_sec013_twilios_caller_id_is_classed_from_the_webhook(form, trust):
    assert screen_mod.trust_from_twilio(form) == trust


async def test_sec013_the_trust_rides_the_call_events_body(monkeypatch):
    run = SimpleNamespace(
        definition=None,
        initial_context={"svp_caller_trust": "withheld"},
        gathered_context={},
        mode="ari",
        workflow_id=3,
        definition_id=4,
        logs={},
    )

    class Db:
        async def get_workflow_run_with_context(self, run_id):
            return run, 7

    import api.db as db_module

    monkeypatch.setattr(db_module, "db_client", Db())
    body = await events._run_body(5)
    assert body["caller_trust"] == "withheld"
    run.initial_context = {}
    assert (await events._run_body(5))["caller_trust"] is None  # stock: nothing said


# --- the ARI path --------------------------------------------------------------------------------


def _ari_connection(monkeypatch, *, channel_trust="network"):
    conn = ari_manager.ARIConnection(
        174, 7, "http://edge-1:8088", "svp", "x", stasis_app_name="dograh_7"
    )
    monkeypatch.setattr(conn, "_get_channel_var", AsyncMock(return_value=channel_trust))
    monkeypatch.setattr(conn, "_delete_channel", AsyncMock())
    db = SimpleNamespace(
        find_active_phone_number_for_inbound=AsyncMock(
            return_value=SimpleNamespace(
                telephony_configuration_id=7,
                inbound_workflow_id=33,
                address="+41445550011",
            )
        ),
        get_workflow=AsyncMock(return_value=SimpleNamespace(id=33, user_id=99)),
        create_workflow_run=AsyncMock(),
    )
    monkeypatch.setattr(ari_manager, "db_client", db)
    concurrency = SimpleNamespace(
        acquire_org_slot=AsyncMock(
            side_effect=CallConcurrencyLimitError(
                organization_id=174, source="ari_inbound", wait_time=0, max_concurrent=1
            )
        ),
        release_slot=AsyncMock(),
    )
    monkeypatch.setattr(ari_manager, "call_concurrency", concurrency)
    return conn, db, concurrency


EVENT = {
    "channel": {
        "id": "chan-1",
        "name": "PJSIP/peoplefone-abc-00000001",
        "caller": {"number": CALLER},
        "dialplan": {"exten": "+41445550011"},
    }
}


async def test_ops001_ari_a_blocked_caller_is_hung_up_unanswered_with_cause_21_and_no_run(
    monkeypatch, switched_on
):
    conn, db, concurrency = _ari_connection(monkeypatch, channel_trust="network")
    asked = AsyncMock(return_value=True)
    monkeypatch.setattr(screen_mod, "screen", asked)
    await conn._handle_inbound_stasis_start("chan-1", "Ring", EVENT)
    conn._delete_channel.assert_awaited_once_with("chan-1", reason_code=21)
    concurrency.acquire_org_slot.assert_not_awaited()
    db.create_workflow_run.assert_not_awaited()
    kwargs = asked.await_args.kwargs
    assert kwargs["organization_id"] == 174 and kwargs["provider"] == "ari"
    assert kwargs["caller_trust"] == "network"  # the edge's SVP_CALLER_TRUST
    conn._get_channel_var.assert_awaited_with("chan-1", "SVP_CALLER_TRUST")


async def test_ops001_ari_an_allowed_caller_goes_on_to_the_stock_path(
    monkeypatch, switched_on
):
    conn, _db, concurrency = _ari_connection(monkeypatch, channel_trust="")
    asked = AsyncMock(return_value=False)
    monkeypatch.setattr(screen_mod, "screen", asked)
    await conn._handle_inbound_stasis_start("chan-1", "Ring", EVENT)
    assert asked.await_args.kwargs["caller_trust"] == "presented"  # no edge word: ours
    concurrency.acquire_org_slot.assert_awaited_once()  # the stock path went on (limit here)
    conn._delete_channel.assert_awaited_once_with(
        "chan-1"
    )  # the stock limit's hangup, no cause


async def test_p51_ari_stock_without_the_switch(monkeypatch):
    monkeypatch.delenv("SVP_CALLER_SCREEN_URL", raising=False)
    conn, _db, concurrency = _ari_connection(monkeypatch)
    asked = AsyncMock(return_value=True)
    monkeypatch.setattr(screen_mod, "screen", asked)
    await conn._handle_inbound_stasis_start("chan-1", "Ring", EVENT)
    asked.assert_not_awaited()
    conn._get_channel_var.assert_not_awaited()
    concurrency.acquire_org_slot.assert_awaited_once()


async def test_p51_the_unanswered_hangup_carries_the_cause(monkeypatch):
    import aiohttp

    calls: list = []

    class Response:
        status = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return ""

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def delete(self, url, auth=None, params=None):
            calls.append((url, params))
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: Session())
    conn = ari_manager.ARIConnection(174, 7, "http://edge-2:8088/", "svp", "x")
    await conn._delete_channel("chan-1", reason_code=21)
    await conn._delete_channel("chan-2")
    assert calls == [
        (
            "http://edge-2:8088/ari/channels/chan-1",
            {"reason_code": "21"},
        ),  # the instance it entered on
        ("http://edge-2:8088/ari/channels/chan-2", None),
    ]


# --- the Twilio path -----------------------------------------------------------------------------


def _twilio_route(*, from_raw=CALLER, verstat=None):
    request = SimpleNamespace(
        headers={}, url="https://voice.example/api/v1/telephony/inbound/run"
    )
    provider_class = SimpleNamespace(
        PROVIDER_NAME="twilio",
        generate_validation_error_response=Mock(return_value="limit-response"),
    )
    raw = {"From": from_raw, "To": "+41445550011", "CallSid": "CA1"}
    if verstat:
        raw["StirVerstat"] = verstat
    normalized = SimpleNamespace(
        provider="twilio",
        direction="inbound",
        to_number="+41445550011",
        from_number=from_raw,
        to_country="CH",
        from_country="CH",
        account_id="AC1",
        call_id="CA1",
        raw_data=raw,
    )
    config = SimpleNamespace(
        id=55, organization_id=11, name="Twilio", credentials={"account_sid": "AC1"}
    )
    phone_row = SimpleNamespace(id=77, address="+41445550011", inbound_workflow_id=33)
    provider_instance = SimpleNamespace(
        verify_inbound_signature=AsyncMock(return_value=True)
    )
    return request, provider_class, normalized, config, phone_row, provider_instance


async def _run_twilio(monkeypatch, refused: bool, **kw):
    from api.routes.telephony import handle_inbound_run

    request, provider_class, normalized, config, phone_row, provider_instance = (
        _twilio_route(**kw)
    )
    asked = AsyncMock(return_value=refused)
    monkeypatch.setattr(screen_mod, "screen", asked)
    with (
        patch(
            "api.routes.telephony.parse_webhook_request",
            new=AsyncMock(return_value=(normalized.raw_data, "raw")),
        ),
        patch(
            "api.routes.telephony._detect_provider",
            new=AsyncMock(return_value=provider_class),
        ),
        patch("api.routes.telephony.normalize_webhook_data", return_value=normalized),
        patch("api.routes.telephony.db_client") as db,
        patch(
            "api.routes.telephony.get_telephony_provider_by_id",
            new=AsyncMock(return_value=provider_instance),
        ),
        patch("api.routes.telephony.call_concurrency") as concurrency,
    ):
        db.find_inbound_route_by_account = AsyncMock(return_value=(config, phone_row))
        db.get_workflow = AsyncMock(return_value=SimpleNamespace(id=33, user_id=99))
        db.create_workflow_run = AsyncMock()
        concurrency.acquire_org_slot = AsyncMock(
            side_effect=CallConcurrencyLimitError(
                organization_id=11,
                source="inbound:twilio",
                wait_time=0,
                max_concurrent=1,
            )
        )
        response = await handle_inbound_run(request)
    return response, asked, db, concurrency


async def test_ops001_twilio_a_blocked_caller_gets_reject_and_no_run(
    monkeypatch, switched_on
):
    response, asked, db, concurrency = await _run_twilio(
        monkeypatch, True, verstat="TN-Validation-Passed-A"
    )
    assert response.media_type == "application/xml"
    assert b'<Reject reason="rejected"/>' in response.body
    assert asked.await_args.kwargs == {
        "organization_id": 11,
        "caller_number": CALLER,
        "called_number": "+41445550011",
        "provider": "twilio",
        "caller_trust": "network",
    }
    concurrency.acquire_org_slot.assert_not_awaited()
    db.create_workflow_run.assert_not_awaited()


async def test_ops001_twilio_an_allowed_caller_goes_on_to_the_stock_path(
    monkeypatch, switched_on
):
    response, asked, _db, concurrency = await _run_twilio(
        monkeypatch, False, from_raw="+266696687"
    )
    assert (
        response == "limit-response"
    )  # the stock path went on to the concurrency limit
    assert asked.await_args.kwargs["caller_trust"] == "withheld"
    concurrency.acquire_org_slot.assert_awaited_once()


async def test_p51_twilio_stock_without_the_switch(monkeypatch):
    monkeypatch.delenv("SVP_CALLER_SCREEN_URL", raising=False)
    response, asked, _db, _concurrency = await _run_twilio(monkeypatch, True)
    asked.assert_not_awaited()
    assert response == "limit-response"


async def test_p51_the_run_keeps_the_trust(monkeypatch):
    from api.routes import telephony as routes

    normalized = SimpleNamespace(
        call_id="CA1",
        from_number=CALLER,
        to_number="+41445550011",
        account_id="AC1",
        from_country="CH",
        to_country="CH",
        raw_data={},
    )
    created = AsyncMock(return_value=SimpleNamespace(id=5))
    with (
        patch("api.routes.telephony.db_client") as db,
        patch(
            "api.routes.telephony.prepare_workflow_run_inputs",
            new=AsyncMock(return_value=SimpleNamespace(definition_id=4)),
        ),
    ):
        db.get_workflow = AsyncMock(return_value=SimpleNamespace(id=33, user_id=99))
        db.create_workflow_run = created
        await routes._create_inbound_workflow_run(
            33, 99, 11, "twilio", normalized, 55, 77, caller_trust="presented"
        )
        assert (
            created.await_args.kwargs["initial_context"]["svp_caller_trust"]
            == "presented"
        )
        await routes._create_inbound_workflow_run(
            33, 99, 11, "twilio", normalized, 55, 77
        )
        assert "svp_caller_trust" not in created.await_args.kwargs["initial_context"]
