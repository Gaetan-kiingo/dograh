"""P-43 (Swiss Voice Platform, ADR-002 / ADR-015 amendment of 2026-10-27; OUT-025, TEL-015,
CF-028): the platform's authorisation verified before a call is placed, the stock single-call
doors closed under the switch, the attempt id on every call event, the edge's outbound dial
string."""

import time

import pytest
from fastapi import HTTPException

from api.services import svp_outbound


def test_p43_stock_triggers_are_closed_only_with_the_switch(monkeypatch):
    monkeypatch.delenv("SVP_OUTBOUND_AUTH", raising=False)
    svp_outbound.refuse_stock_trigger()  # stock: nothing happens
    monkeypatch.setenv("SVP_OUTBOUND_AUTH", "on")
    with pytest.raises(HTTPException) as e:
        svp_outbound.refuse_stock_trigger()
    assert e.value.status_code == 404  # indistinguishable from an absent route


def test_p43_the_platforms_signature_is_accepted_and_bound_to_the_call(monkeypatch):
    monkeypatch.setenv("SVP_KEY_SERVICE_TOKEN", "dev-only-key-service-token")
    expires = int(time.time()) + 60
    header = svp_outbound.sign(4242, 7, "+41791234567", "attempt-1", expires)
    assert header.startswith(f"v1={expires}.")
    svp_outbound.verify_authorization(
        header, organization_id=4242, workflow_id=7, phone="+41791234567", attempt_id="attempt-1"
    )
    # another number, another attempt, another workflow or another organisation: refused
    for kwargs in (
        dict(organization_id=4242, workflow_id=7, phone="+41790000000", attempt_id="attempt-1"),
        dict(organization_id=4242, workflow_id=7, phone="+41791234567", attempt_id="attempt-2"),
        dict(organization_id=4242, workflow_id=8, phone="+41791234567", attempt_id="attempt-1"),
        dict(organization_id=4243, workflow_id=7, phone="+41791234567", attempt_id="attempt-1"),
    ):
        with pytest.raises(HTTPException) as e:
            svp_outbound.verify_authorization(header, **kwargs)
        assert e.value.status_code == 403


def test_p43_an_expired_missing_or_malformed_authorization_is_refused(monkeypatch):
    monkeypatch.setenv("SVP_KEY_SERVICE_TOKEN", "dev-only-key-service-token")
    old = svp_outbound.sign(4242, 7, "+41791234567", "a", int(time.time()) - 1)
    for header in (old, None, "", "v2=1.abc", "v1=notanumber.abc", "v1=99999999999"):
        with pytest.raises(HTTPException) as e:
            svp_outbound.verify_authorization(
                header, organization_id=4242, workflow_id=7, phone="+41791234567", attempt_id="a"
            )
        assert e.value.status_code == 403
    # no shared secret in the runtime's environment: nothing can be verified, fail closed
    monkeypatch.delenv("SVP_KEY_SERVICE_TOKEN", raising=False)
    fresh = svp_outbound.sign(4242, 7, "+41791234567", "a", int(time.time()) + 60)
    with pytest.raises(HTTPException):
        svp_outbound.verify_authorization(
            fresh, organization_id=4242, workflow_id=7, phone="+41791234567", attempt_id="a"
        )


def test_p43_the_edge_decides_how_a_destination_is_dialled(monkeypatch):
    monkeypatch.delenv("SVP_ARI_OUTBOUND_DIAL", raising=False)
    assert svp_outbound.outbound_dial_string("+41791234567") == "PJSIP/+41791234567"
    monkeypatch.setenv("SVP_ARI_OUTBOUND_DIAL", "Local/{destination}@svp-outbound")
    assert svp_outbound.outbound_dial_string("+41791234567") == "Local/+41791234567@svp-outbound"
    # a destination already written as a channel is left alone
    assert svp_outbound.outbound_dial_string("PJSIP/softphone") == "PJSIP/softphone"
