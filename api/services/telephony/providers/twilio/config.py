"""Twilio telephony configuration schemas."""

from typing import Literal

from pydantic import BaseModel, Field


class TwilioConfigurationRequest(BaseModel):
    """Request schema for Twilio configuration."""

    provider: Literal["twilio"] = Field(default="twilio")
    account_sid: str = Field(..., description="Twilio Account SID")
    auth_token: str = Field(..., description="Twilio Auth Token")
    # P-41 (Swiss Voice Platform, ADR-053 amendment of 2026-10-27; CF-172): the Twilio
    # Region the account's credentials were created in - Twilio's Regions are isolated,
    # credentials included, and a number routed to Ireland ignores the US1 configuration.
    # The REST host and the webhook signatures follow it. Absent = us1, stock.
    region: Literal["us1", "ie1", "au1"] = Field(
        default="us1", description="Twilio Region of these credentials (us1, ie1, au1)"
    )
    amd_enabled: bool = Field(
        default=False,
        description=(
            "Detect whether outbound calls are answered by a person or machine. "
            "Twilio may bill AMD as an additional per-call feature."
        ),
    )
