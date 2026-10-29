"""P-04 (Swiss Voice Platform, ADR-053 amendment of 2026-10-29): several ARI endpoints on
one telephony configuration - two edge instances, one connection each, a channel served
by the instance it entered on."""

import os
from unittest import mock

from api.services.telephony import ari_endpoints


def test_p04_a_configurations_endpoints_are_one_unless_switched_on():
    with mock.patch.dict(os.environ, {"SVP_ARI_ENDPOINTS": ""}):
        assert ari_endpoints.split_ari_endpoints("http://a:8088/, http://b:8088") == [
            "http://a:8088/, http://b:8088"
        ]  # stock: the value as written
    with mock.patch.dict(os.environ, {"SVP_ARI_ENDPOINTS": "on"}):
        assert ari_endpoints.split_ari_endpoints("http://a:8088/, http://b:8088") == [
            "http://a:8088",
            "http://b:8088",
        ]
        assert ari_endpoints.split_ari_endpoints("http://a:8088") == ["http://a:8088"]
        assert ari_endpoints.split_ari_endpoints("") == [""]


def test_p04_one_connection_per_endpoint_with_its_own_key():
    from api.services.telephony.ari_manager import ARIConnection

    with mock.patch.dict(os.environ, {"SVP_ARI_ENDPOINTS": "on"}):
        a = ARIConnection(1, 7, "http://a:8088/", "svp", "x", stasis_app_name="dograh_7")
        b = ARIConnection(1, 7, "http://b:8088", "svp", "x", stasis_app_name="dograh_7")
        assert a.connection_key == "config:7:http://a:8088"
        assert b.connection_key == "config:7:http://b:8088"
        assert a.stasis_app_name == b.stasis_app_name == "dograh_7"
    with mock.patch.dict(os.environ, {"SVP_ARI_ENDPOINTS": ""}):
        assert a.connection_key == "config:7"


def test_p04_a_channel_is_served_by_the_instance_it_entered_on():
    from api.services.telephony.providers.ari.provider import ARIProvider

    with mock.patch.dict(os.environ, {"SVP_ARI_ENDPOINTS": "on"}):
        provider = ARIProvider(
            {"ari_endpoint": "http://a:8088,http://b:8088", "app_name": "svp", "app_password": "x"}
        )
        assert provider.ari_endpoints == ["http://a:8088", "http://b:8088"]
        assert provider.base_url == "http://a:8088/ari"  # outbound originates: the first
        ari_endpoints.remember_home("chan-1", "http://b:8088/")
        assert provider._base_for("chan-1") == "http://b:8088/ari"
        assert provider._base_for("chan-unknown") == "http://a:8088/ari"
        # the handoff names the instance the call lives on
        assert provider._base_for(endpoint="http://b:8088") == "http://b:8088/ari"
        ari_endpoints.forget_home("chan-1")
        assert provider._base_for("chan-1") == "http://a:8088/ari"
    with mock.patch.dict(os.environ, {"SVP_ARI_ENDPOINTS": ""}):
        ari_endpoints.remember_home("chan-2", "http://b:8088")  # stock: nothing remembered
        assert ari_endpoints.home_of("chan-2") is None
