"""P-55 (Swiss Voice Platform, ADR-002; E2b, ADR-083 amendment; BIL-006): the platform's door
to an organisation's simultaneous calls - see ``api.services.svp_plan_limits``."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, StrictInt

from api.services import svp_plan_limits


def _platform_only(authorization: Annotated[str | None, Header()] = None) -> None:
    """404 without the switch, 401 without the platform's bearer - both before the body is
    read."""
    if not svp_plan_limits.enabled():
        raise HTTPException(status_code=404, detail="Not found")
    if not svp_plan_limits.platform_bearer_ok(authorization):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(
    prefix="/svp",
    tags=["svp"],
    include_in_schema=False,
    dependencies=[Depends(_platform_only)],
)


Limit = Annotated[
    StrictInt, Field(ge=svp_plan_limits.LIMIT_MIN, le=svp_plan_limits.LIMIT_MAX)
]


class ConcurrentCallLimit(BaseModel):
    value: Limit | None  # required; null = the runtime's default


class ConcurrentCallLimitResponse(BaseModel):
    organization_id: int
    value: int | None


@router.put(
    "/organizations/{organization_id}/concurrent-call-limit",
    response_model=ConcurrentCallLimitResponse,
)
async def put_concurrent_call_limit(
    organization_id: int, request: ConcurrentCallLimit
) -> ConcurrentCallLimitResponse:
    if not await svp_plan_limits.set_concurrent_call_limit(
        organization_id, request.value
    ):
        raise HTTPException(status_code=404, detail="Organization not found")
    return ConcurrentCallLimitResponse(
        organization_id=organization_id, value=request.value
    )
