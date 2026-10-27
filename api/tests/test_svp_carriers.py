"""D2a (Swiss Voice Platform, ADR-053; ADR-002 P-34, P-35, P-36). P-34: a carrier's secret is
stored by reference and resolved from the runtime's environment, never used as text. P-35:
the number reports every inbound call's terminal status to a route that finds the run by
the carrier's call id; the call events carry the carrier's facts (TEL-006). P-36 (CF-062):
log messages have phone numbers and e-mail addresses masked; the trace is kept."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services import svp_call_events as events
from api.services.telephony import factory
from api.services.telephony.providers.twilio import provider as twilio_provider


# --- P-34 ----------------------------------------------------------------------------------------


def test_p34_references_are_resolved_from_the_environment_only_with_the_switch(monkeypatch):
    monkeypatch.setenv("SVP_PROVIDER_TWILIO_AUTH_TOKEN", "the-real-token")
    config = {"provider": "twilio", "account_sid": "AC123", "auth_token": "secretref:SVP_PROVIDER_TWILIO_AUTH_TOKEN"}
    monkeypatch.delenv("SVP_TELEPHONY_SECRET_REFS", raising=False)
    assert factory.resolve_telephony_secret_references(config)["auth_token"].startswith("secretref:")
    monkeypatch.setenv("SVP_TELEPHONY_SECRET_REFS", "on")
    resolved = factory.resolve_telephony_secret_references(config)
    assert resolved["auth_token"] == "the-real-token"
    assert resolved["account_sid"] == "AC123"  # literal: the inbound lookup matches on it
    assert "the-real-token" not in str(config)  # the stored configuration is untouched


def test_p34_a_missing_secret_fails_closed(monkeypatch):
    monkeypatch.setenv("SVP_TELEPHONY_SECRET_REFS", "on")
    monkeypatch.delenv("SVP_PROVIDER_TWILIO_AUTH_TOKEN", raising=False)
    with pytest.raises(Exception, match="not set in the runtime's environment"):
        factory.resolve_telephony_secret_references({"auth_token": "secretref:SVP_PROVIDER_TWILIO_AUTH_TOKEN"})
    with pytest.raises(Exception, match="not allowed"):
        factory.resolve_telephony_secret_references({"auth_token": "secretref:HOME"})


# --- P-35 ----------------------------------------------------------------------------------------


class _Session:
    posted: list[dict] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def post(self, endpoint, data=None, auth=None):
        _Session.posted.append({"endpoint": endpoint, "data": dict(data or {})})
        return _Response()


class _Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def text(self):
        return ""


@pytest.mark.asyncio
async def test_p35_the_number_reports_every_calls_status_when_switched_on(monkeypatch):
    _Session.posted = []
    monkeypatch.setattr(twilio_provider.aiohttp, "ClientSession", _Session)
    prov = twilio_provider.TwilioProvider({"account_sid": "AC1", "auth_token": "t", "from_numbers": []})
    monkeypatch.setattr(prov, "_lookup_incoming_number_sid", AsyncMock(return_value="PN1"))
    webhook = "https://voice.example.ch/api/v1/telephony/inbound/run"
    monkeypatch.delenv("SVP_CARRIER_STATUS", raising=False)
    assert (await prov.configure_inbound("+41445550000", webhook)).ok
    assert "StatusCallback" not in _Session.posted[-1]["data"]  # stock
    monkeypatch.setenv("SVP_CARRIER_STATUS", "on")
    assert (await prov.configure_inbound("+41445550000", webhook)).ok
    data = _Session.posted[-1]["data"]
    assert data["VoiceUrl"] == webhook
    assert data["StatusCallback"] == "https://voice.example.ch/api/v1/telephony/twilio/status"
    assert data["StatusCallbackMethod"] == "POST"
    # clearing the number clears the voice URL only (stock shape)
    assert (await prov.configure_inbound("+41445550000", None)).ok
    assert _Session.posted[-1]["data"] == {"VoiceUrl": ""}


@pytest.mark.asyncio
async def test_p35_the_status_route_finds_the_run_by_the_carriers_call_id(monkeypatch):
    from api.services.telephony.providers.twilio import routes

    run = SimpleNamespace(id=77, workflow_id=9)
    processed: list = []
    fake_db = SimpleNamespace(
        get_workflow_run_by_call_id=AsyncMock(return_value=run),
        get_workflow_by_id=AsyncMock(return_value=SimpleNamespace(organization_id=5)),
    )
    prov = SimpleNamespace(
        verify_inbound_signature=AsyncMock(return_value=True),
        parse_status_callback=lambda d: {"call_id": d["CallSid"], "status": d["CallStatus"], "duration": d.get("CallDuration")},
    )
    monkeypatch.setattr(routes, "db_client", fake_db)
    monkeypatch.setattr(routes, "get_telephony_provider_for_run", AsyncMock(return_value=prov))

    async def record(run_id, update):
        processed.append((run_id, update.status, update.duration))

    monkeypatch.setattr(routes, "_process_status_update", record)

    class Req:
        url = "https://voice.example.ch/api/v1/telephony/twilio/status"
        headers = {"x-twilio-signature": "sig"}

        def __init__(self, form):
            self._form = form

        async def form(self):
            return self._form

    answer = await routes.handle_twilio_number_status(Req({"CallSid": "CA1", "CallStatus": "no-answer", "CallDuration": "0"}))
    assert answer == {"status": "success"}
    assert processed == [(77, "no-answer", "0")]
    fake_db.get_workflow_run_by_call_id.assert_awaited_with("CA1")
    # a call the runtime never knew is acknowledged, not an error
    fake_db.get_workflow_run_by_call_id = AsyncMock(return_value=None)
    assert (await routes.handle_twilio_number_status(Req({"CallSid": "CA9", "CallStatus": "failed"})))["status"] == "ignored"
    assert (await routes.handle_twilio_number_status(Req({"CallStatus": "failed"})))["reason"] == "no_call_sid"


@pytest.mark.asyncio
async def test_p35_an_unconnected_call_tells_the_platform_the_carriers_facts(monkeypatch):
    from api.services.telephony import status_processor as sp

    sent: list[dict] = []

    async def ended_now(run_id, reason, **extra):
        sent.append({"run_id": run_id, "reason": reason, **extra})
        return True

    monkeypatch.setattr(events, "ended_now", ended_now)
    with patch.object(sp, "enqueue_job", new_callable=AsyncMock):
        await sp._enqueue_integrations_for_unconnected_run(77, "busy", duration="0", call_id="CA1")
    assert sent == [{"run_id": 77, "reason": "carrier_busy", "carrier_status": "busy", "carrier_duration": "0", "carrier_call_id": "CA1"}]


@pytest.mark.asyncio
async def test_tel006_the_call_events_carry_the_carriers_call_id_and_the_codec(monkeypatch):
    import api.db as db_module

    class Db:
        async def get_workflow_run_with_context(self, run_id):
            return SimpleNamespace(
                workflow_id=9, definition_id=3, definition=None, mode="twilio",
                initial_context={"caller_number": "+41791234567", "called_number": "+41445550000", "provider": "twilio", "telephony_configuration_id": 12},
                gathered_context={"call_id": "CA1"},
            ), 5

    monkeypatch.setattr(db_module, "db_client", Db())
    body = await events._run_body(77)
    assert body["carrier_call_id"] == "CA1"
    assert body["telephony_configuration_id"] == 12
    assert body["codec"] == "PCMU/8000"
    assert events.CODEC_BY_MODE["smallwebrtc"].startswith("opus")


# --- P-36 (CF-062) -------------------------------------------------------------------------------


def test_p36_log_messages_mask_phone_numbers_and_addresses_only_with_the_switch(monkeypatch):
    from api import logging_config as lc

    text = "ARI Inbound +41 79 123 45 67 from jean.dupont@example.ch; run 1494 at 13:30"
    assert lc.redact(text) == "ARI Inbound …67 from …@example.ch; run 1494 at 13:30"
    record = {"extra": {}, "level": SimpleNamespace(no=20), "message": text}
    monkeypatch.delenv("SVP_LOG_REDACT", raising=False)
    lc.enrich_log_record(record)
    assert record["message"] == text  # stock
    monkeypatch.setenv("SVP_LOG_REDACT", "on")
    lc.enrich_log_record(record)
    assert "+41 79" not in record["message"] and "jean.dupont" not in record["message"]
    assert "1494" in record["message"]  # a run id is not a phone number


def test_p36_the_trace_is_kept_on_the_record():
    from api import logging_config as lc

    token = lc.trace_id_var.set("9c74c9ca4fd51155691441f6db7f2109")
    try:
        record = {"extra": {}, "level": SimpleNamespace(no=20), "message": "x"}
        lc.enrich_log_record(record)
        assert record["extra"]["trace_id"] == "9c74c9ca4fd51155691441f6db7f2109"
    finally:
        lc.trace_id_var.reset(token)


# --- P-34, bring your own carrier: the workspace's credential fetched from the platform ---------


@pytest.mark.asyncio
async def test_p34_a_reference_the_environment_lacks_is_asked_of_the_platform_for_the_organisation(monkeypatch):
    from api.services.configuration import secret_refs

    monkeypatch.setenv("SVP_TELEPHONY_SECRET_REFS", "on")
    monkeypatch.delenv("SVP_PROVIDER_TWILIO_" + "A" * 32, raising=False)
    asked: list[dict] = []

    async def fetch(names, *, organization_id, workflow_id, run_id):
        asked.append({"names": names, "organization_id": organization_id, "run_id": run_id})
        return {names[0]: "the-tenants-token"}

    monkeypatch.setattr(secret_refs, "_fetch", fetch)
    factory._CARRIER_SECRETS.clear()
    name = "secretref:SVP_PROVIDER_TWILIO_" + "A" * 32
    config = {"provider": "twilio", "account_sid": "AC1", "auth_token": name}
    resolved = await factory.resolve_telephony_secret_references_for_org(config, 5)
    assert resolved["auth_token"] == "the-tenants-token" and resolved["account_sid"] == "AC1"
    assert asked == [{"names": ["SVP_PROVIDER_TWILIO_" + "A" * 32], "organization_id": 5, "run_id": None}]
    # kept for a while: the next inbound webhook does not ask again
    await factory.resolve_telephony_secret_references_for_org(config, 5)
    assert len(asked) == 1
    # another organisation asks for its own
    await factory.resolve_telephony_secret_references_for_org(config, 6)
    assert len(asked) == 2 and asked[1]["organization_id"] == 6
    # the environment wins when it holds the name (Driing's own account)
    monkeypatch.setenv("SVP_PROVIDER_TWILIO_" + "A" * 32, "env-token")
    factory._CARRIER_SECRETS.clear()
    assert (await factory.resolve_telephony_secret_references_for_org(config, 7))["auth_token"] == "env-token"
    assert len(asked) == 2


def test_p41_the_rest_host_follows_the_configurations_region_with_the_switch(monkeypatch):
    """ADR-053 amendment of 2026-10-27 (CF-172): Twilio's Regions are isolated, credentials
    included - the owner's Ireland number ignored the US1 configuration and its webhooks
    failed the signature check with the US token."""
    from api.services.telephony.providers.twilio.provider import TwilioProvider, twilio_api_host

    monkeypatch.setenv("SVP_TWILIO_REGIONS", "on")
    assert twilio_api_host("ie1") == "api.dublin.ie1.twilio.com"
    assert twilio_api_host("au1") == "api.sydney.au1.twilio.com"
    assert twilio_api_host("us1") == "api.twilio.com"
    assert twilio_api_host("") == "api.twilio.com"
    sid = "AC" + "0" * 32
    irish = TwilioProvider({"account_sid": sid, "auth_token": "t", "region": "ie1"})
    assert irish.base_url == f"https://api.dublin.ie1.twilio.com/2010-04-01/Accounts/{sid}"
    stock = TwilioProvider({"account_sid": sid, "auth_token": "t"})
    assert stock.region == "us1" and stock.base_url.startswith("https://api.twilio.com/")
    # unset = stock, whatever the configuration says
    monkeypatch.delenv("SVP_TWILIO_REGIONS")
    assert TwilioProvider({"account_sid": sid, "auth_token": "t", "region": "ie1"}).base_url.startswith(
        "https://api.twilio.com/"
    )


def test_p41_the_configuration_request_takes_a_region():
    from api.services.telephony.providers.twilio import SPEC
    from api.services.telephony.providers.twilio.config import TwilioConfigurationRequest

    request = TwilioConfigurationRequest(account_sid="AC" + "1" * 32, auth_token="t", region="ie1")
    assert request.region == "ie1"
    assert TwilioConfigurationRequest(account_sid="AC" + "1" * 32, auth_token="t").region == "us1"
    assert SPEC.config_loader({"account_sid": "x", "auth_token": "y", "region": "ie1"})["region"] == "ie1"
    assert SPEC.config_loader({"account_sid": "x", "auth_token": "y"})["region"] == "us1"


def test_p34_a_number_whose_credential_is_gone_is_still_removed(monkeypatch):
    """ADR-053 amendment of 2026-10-27 (the owner's revoke on the VM): the platform no longer
    serves a revoked connection's token, so the runtime cannot detach the number at the
    carrier - the row goes all the same, the answer says the carrier was not told."""
    from api.routes.organization import row_removed_without_the_carrier

    monkeypatch.setenv("SVP_TELEPHONY_SECRET_REFS", "on")
    assert row_removed_without_the_carrier(
        "Provider load failed: the platform refused the provider keys (409 secret_not_found)"
    )
    # a carrier that answered and refused is still a refusal
    assert not row_removed_without_the_carrier("Twilio VoiceUrl update failed for …08 (401)")
    assert not row_removed_without_the_carrier(None)
    monkeypatch.delenv("SVP_TELEPHONY_SECRET_REFS")
    assert not row_removed_without_the_carrier("Provider load failed: x")
