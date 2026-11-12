"""P-55 (Swiss Voice Platform, ADR-002; E2b, ADR-083 amendment; BIL-006, BIL-007): a plan's
simultaneous calls - the platform sets an organisation's limit through its own door, a phone
call over the limit hears busy (cause 17 on the edge, <Reject reason="busy"/> on Twilio), and
the member's own browser test takes no slot while the website widget does."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.constants import DEFAULT_ORG_CONCURRENCY_LIMIT
from api.services import svp_plan_limits
from api.services.call_concurrency import call_concurrency
from api.services.call_concurrency import service as concurrency_service

TOKEN = "bearer-of-the-platform"
URL = "/api/v1/svp/organizations/{}/concurrent-call-limit"


@pytest.fixture
def switched_on(monkeypatch):
    monkeypatch.setenv("SVP_PLAN_LIMITS", "on")
    monkeypatch.setenv("SVP_KEY_SERVICE_TOKEN", TOKEN)


@pytest.fixture
def switched_off(monkeypatch):
    monkeypatch.delenv("SVP_PLAN_LIMITS", raising=False)
    monkeypatch.setenv("SVP_KEY_SERVICE_TOKEN", TOKEN)


class Configurations:
    """The organisations and their configuration rows, in memory - the db client's double
    for the door and for call_concurrency's read."""

    def __init__(self, organisations=(7,)):
        self.organisations = set(organisations)
        self.rows: dict[tuple[int, str], dict] = {}

    async def get_organization_by_id(self, organization_id):
        return (
            SimpleNamespace(id=organization_id)
            if organization_id in self.organisations
            else None
        )

    async def upsert_configuration(
        self, organization_id, key, value, last_validated_at=None
    ):
        self.rows[(organization_id, key)] = value
        return SimpleNamespace(value=value)

    async def delete_configuration(self, organization_id, key):
        return self.rows.pop((organization_id, key), None) is not None

    async def get_configuration(self, organization_id, key):
        value = self.rows.get((organization_id, key))
        return SimpleNamespace(value=value) if value is not None else None


@pytest.fixture
def db(monkeypatch):
    double = Configurations()
    monkeypatch.setattr(svp_plan_limits, "db_client", double)
    monkeypatch.setattr(concurrency_service, "db_client", double)
    return double


def _client() -> AsyncClient:
    """The runtime's own router, mounted as app.py mounts it - the door's path is proven."""
    from api.routes.main import router as main_router

    app = FastAPI()
    app.include_router(main_router, prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime")


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- (1) the platform's door ---------------------------------------------------------------------


async def test_p55_the_door_is_absent_without_the_switch(switched_off, db):
    async with _client() as client:
        response = await client.put(URL.format(7), json={"value": 3}, headers=_auth())
    assert response.status_code == 404
    assert db.rows == {}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": TOKEN},  # no scheme
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer "},
    ],
)
async def test_p55_the_door_refuses_anyone_but_the_platform(switched_on, db, headers):
    async with _client() as client:
        response = await client.put(URL.format(7), json={"value": 3}, headers=headers)
    assert response.status_code == 401
    assert db.rows == {}


async def test_p55_the_door_fails_closed_when_the_runtime_has_no_token(
    switched_on, db, monkeypatch
):
    monkeypatch.setenv("SVP_KEY_SERVICE_TOKEN", "")
    async with _client() as client:
        response = await client.put(URL.format(7), json={"value": 3}, headers=_auth(""))
    assert response.status_code == 401
    assert db.rows == {}


async def test_p55_the_bearer_is_checked_before_the_body(switched_on, db):
    async with _client() as client:
        response = await client.put(URL.format(7), json={"value": "nonsense"})
    assert response.status_code == 401


async def test_bil006_the_platform_sets_the_limit_and_the_next_slot_reads_it(
    switched_on, db
):
    assert await call_concurrency.get_org_concurrent_limit(7) == int(
        DEFAULT_ORG_CONCURRENCY_LIMIT
    )
    async with _client() as client:
        response = await client.put(URL.format(7), json={"value": 3}, headers=_auth())
    assert response.status_code == 200
    assert response.json() == {"organization_id": 7, "value": 3}
    assert db.rows == {
        (7, "CONCURRENT_CALL_LIMIT"): {"value": 3}
    }  # the stock configuration
    assert await call_concurrency.get_org_concurrent_limit(7) == 3

    async with _client() as client:
        response = await client.put(URL.format(7), json={"value": 1}, headers=_auth())
    assert response.json() == {"organization_id": 7, "value": 1}
    assert (
        await call_concurrency.get_org_concurrent_limit(7) == 1
    )  # no cache: read at every slot


async def test_bil006_null_gives_the_runtime_default_back(switched_on, db):
    async with _client() as client:
        await client.put(URL.format(7), json={"value": 3}, headers=_auth())
        response = await client.put(
            URL.format(7), json={"value": None}, headers=_auth()
        )
        again = await client.put(URL.format(7), json={"value": None}, headers=_auth())
    assert response.status_code == 200 and again.status_code == 200
    assert response.json() == {"organization_id": 7, "value": None}
    assert db.rows == {}
    assert await call_concurrency.get_org_concurrent_limit(7) == int(
        DEFAULT_ORG_CONCURRENCY_LIMIT
    )


async def test_p55_an_unknown_organisation_is_404_and_nothing_is_written(
    switched_on, db
):
    async with _client() as client:
        response = await client.put(URL.format(8), json={"value": 3}, headers=_auth())
    assert response.status_code == 404
    assert db.rows == {}


@pytest.mark.parametrize(
    "body",
    [
        {"value": 0},
        {"value": 1001},
        {"value": -1},
        {"value": "3"},
        {"value": 2.5},
        {"value": True},
        {},
    ],
)
async def test_p55_only_a_limit_from_1_to_1000_or_null_is_accepted(
    switched_on, db, body
):
    async with _client() as client:
        response = await client.put(URL.format(7), json=body, headers=_auth())
    assert response.status_code == 422
    assert db.rows == {}


# --- (2) a phone call over the limit hears busy --------------------------------------------------


async def test_bil006_ari_a_call_over_the_limit_is_hung_up_with_cause_17(
    switched_on, monkeypatch
):
    from api.services import svp_caller_screen as screen_mod
    from api.tests.test_svp_caller_screen import EVENT, _ari_connection

    monkeypatch.delenv("SVP_CALLER_SCREEN_URL", raising=False)
    conn, db_double, concurrency = _ari_connection(monkeypatch)
    monkeypatch.setattr(screen_mod, "screen", AsyncMock(return_value=False))
    await conn._handle_inbound_stasis_start("chan-1", "Ring", EVENT)
    concurrency.acquire_org_slot.assert_awaited_once()
    conn._delete_channel.assert_awaited_once_with("chan-1", reason_code=17)
    db_double.create_workflow_run.assert_not_awaited()


async def test_p55_ari_stock_without_the_switch(switched_off, monkeypatch):
    from api.tests.test_svp_caller_screen import EVENT, _ari_connection

    monkeypatch.delenv("SVP_CALLER_SCREEN_URL", raising=False)
    conn, _db, _concurrency = _ari_connection(monkeypatch)
    await conn._handle_inbound_stasis_start("chan-1", "Ring", EVENT)
    conn._delete_channel.assert_awaited_once_with(
        "chan-1"
    )  # the stock hangup, no cause


async def test_bil006_twilio_a_call_over_the_limit_gets_reject_busy(
    switched_on, monkeypatch
):
    from api.tests.test_svp_caller_screen import _run_twilio

    monkeypatch.delenv("SVP_CALLER_SCREEN_URL", raising=False)
    response, _asked, db_double, concurrency = await _run_twilio(monkeypatch, False)
    concurrency.acquire_org_slot.assert_awaited_once()
    assert response.media_type == "application/xml"
    assert response.body == (
        b'<?xml version="1.0" encoding="UTF-8"?><Response><Reject reason="busy"/></Response>'
    )
    db_double.create_workflow_run.assert_not_awaited()


async def test_p55_twilio_stock_without_the_switch(switched_off, monkeypatch):
    from api.tests.test_svp_caller_screen import _run_twilio

    monkeypatch.delenv("SVP_CALLER_SCREEN_URL", raising=False)
    response, _asked, _db, _concurrency = await _run_twilio(monkeypatch, False)
    assert response == "limit-response"  # the provider's stock error answer


async def test_p55_another_provider_keeps_its_stock_answer(switched_on, monkeypatch):
    from api.routes.telephony import handle_inbound_run
    from api.services.call_concurrency import CallConcurrencyLimitError
    from api.tests.test_svp_caller_screen import _twilio_route

    monkeypatch.delenv("SVP_CALLER_SCREEN_URL", raising=False)
    request, _pc, normalized, config, phone_row, provider_instance = _twilio_route()
    provider_class = SimpleNamespace(
        PROVIDER_NAME="vonage",
        generate_validation_error_response=Mock(return_value="limit-response"),
    )
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
        patch("api.routes.telephony.db_client") as db_double,
        patch(
            "api.routes.telephony.get_telephony_provider_by_id",
            new=AsyncMock(return_value=provider_instance),
        ),
        patch("api.routes.telephony.call_concurrency") as concurrency,
    ):
        db_double.find_inbound_route_by_account = AsyncMock(
            return_value=(config, phone_row)
        )
        db_double.get_workflow = AsyncMock(
            return_value=SimpleNamespace(id=33, user_id=99)
        )
        concurrency.acquire_org_slot = AsyncMock(
            side_effect=CallConcurrencyLimitError(
                organization_id=11,
                source="inbound:vonage",
                wait_time=0,
                max_concurrent=1,
            )
        )
        response = await handle_inbound_run(request)
    assert response == "limit-response"


# --- (3) the member's own browser test takes no slot ---------------------------------------------


async def _public_door(monkeypatch, initial_context):
    from api.routes.webrtc_signaling import public_signaling_websocket
    from api.tests.test_public_signaling_origin import _FakeWebSocket, _patch_deps

    ws = _FakeWebSocket("https://example.com")
    db_double, manager = _patch_deps()
    db_double.get_workflow_run = AsyncMock(
        return_value=SimpleNamespace(
            id=42, workflow_id=3, mode="smallwebrtc", initial_context=initial_context
        )
    )
    try:
        await public_signaling_websocket(ws, "emb_session_tok")
    finally:
        patch.stopall()
    manager.handle_websocket.assert_awaited_once()
    return manager.handle_websocket.await_args.kwargs


@pytest.mark.parametrize(
    "initial_context, counted",
    [
        (
            {"source": "svp-voice-test", "provider": "smallwebrtc"},
            False,
        ),  # the member's test
        (
            {"source": "svp-widget", "provider": "smallwebrtc"},
            True,
        ),  # the website widget
        ({"provider": "smallwebrtc"}, True),  # no source: stock
        (None, True),
    ],
)
async def test_bil006_the_members_test_takes_no_slot_the_widget_does(
    switched_on, monkeypatch, initial_context, counted
):
    kwargs = await _public_door(monkeypatch, initial_context)
    assert kwargs["enforce_call_concurrency"] is counted
    assert kwargs["call_concurrency_source"] == "public_embed"


@pytest.mark.parametrize("source", ["svp-voice-test", "svp-widget"])
async def test_p55_without_the_switch_every_browser_call_is_counted(
    switched_off, monkeypatch, source
):
    kwargs = await _public_door(monkeypatch, {"source": source})
    assert kwargs["enforce_call_concurrency"] is True


async def test_bil006_an_uncounted_test_takes_and_leaves_no_slot(switched_on):
    """With enforcement off for the run, the offer never acquires - and the pipeline's
    release finds no mapping to undo."""
    from api.routes.webrtc_signaling import SignalingManager

    manager = SignalingManager()
    ws = SimpleNamespace(send_json=AsyncMock())
    with (
        patch("api.routes.webrtc_signaling.db_client") as db_double,
        patch(
            "api.routes.webrtc_signaling.authorize_workflow_run_start",
            new=AsyncMock(
                return_value=SimpleNamespace(has_quota=True, error_message="")
            ),
        ),
        patch("api.routes.webrtc_signaling.call_concurrency") as concurrency,
        patch(
            "api.routes.webrtc_signaling.SmallWebRTCConnection",
            side_effect=RuntimeError("stop after the slot decision"),
        ),
    ):
        db_double.get_workflow_run = AsyncMock(
            return_value=SimpleNamespace(id=42, is_completed=False)
        )
        concurrency.acquire_org_slot = AsyncMock()
        concurrency.release_slot = AsyncMock()
        concurrency.release_workflow_run_slot = AsyncMock()
        with pytest.raises(RuntimeError):
            await manager._handle_offer(
                ws,
                {"pc_id": "pc-1", "sdp": "v=0\r\n", "type": "offer"},
                workflow_id=3,
                workflow_run_id=42,
                user=SimpleNamespace(id=7),
                organization_id=11,
                connection_key="conn-1",
                enforce_call_concurrency=False,
                call_concurrency_source="public_embed",
            )
    concurrency.acquire_org_slot.assert_not_called()
    concurrency.release_slot.assert_not_called()
    concurrency.release_workflow_run_slot.assert_not_called()
