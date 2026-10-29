"""P-04 (Swiss Voice Platform, ADR-002; ADR-053 amendment of 2026-10-29, D2b slice 2 and D7):
a telephony configuration may name SEVERAL ARI endpoints, comma-separated - two edge
instances serving the same Stasis application with the same credentials. The manager
holds one connection per endpoint; a channel is served by the instance it entered on,
remembered here, which the provider's per-channel calls and the handoff's originate use.
`SVP_ARI_ENDPOINTS` unset = stock: one endpoint, the value as written."""

import os


def ari_endpoints_enabled() -> bool:
    return os.environ.get("SVP_ARI_ENDPOINTS", "").strip().lower() in ("on", "1", "true")


def split_ari_endpoints(value: str | None) -> list[str]:
    """The endpoints of a configuration's `ari_endpoint` value, trailing slashes dropped."""
    raw = str(value or "")
    if not ari_endpoints_enabled():
        return [raw.rstrip("/")]
    parts = [p.strip().rstrip("/") for p in raw.split(",")]
    return [p for p in parts if p] or [raw.rstrip("/")]


# channel id -> the ARI endpoint it lives on (set at StasisStart, dropped at ChannelDestroyed)
CHANNEL_HOMES: dict[str, str] = {}


def remember_home(channel_id: str, endpoint: str) -> None:
    if ari_endpoints_enabled() and channel_id:
        CHANNEL_HOMES[channel_id] = endpoint.rstrip("/")


def forget_home(channel_id: str) -> None:
    CHANNEL_HOMES.pop(channel_id, None)


def home_of(channel_id: str | None) -> str | None:
    return CHANNEL_HOMES.get(channel_id or "")
