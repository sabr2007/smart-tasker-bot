# src/web/routes/users.py
"""User settings API endpoints."""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

import db
from web.deps import get_current_user

# Common timezone options for UI
COMMON_TIMEZONES = [
    "Asia/Almaty",
    "Asia/Tashkent",
    "Asia/Bishkek",
    "Europe/Moscow",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "America/New_York",
    "America/Los_Angeles",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Dubai",
    "Australia/Sydney",
    "Asia/Ho_Chi_Minh",
]

router = APIRouter(prefix="/api/users", tags=["users"])


class UserSettingsOut(BaseModel):
    user_id: int
    timezone: str


class UserSettingsPatch(BaseModel):
    timezone: str = Field(..., min_length=1, max_length=100)


class TimezoneListOut(BaseModel):
    common: list[str]


def _validate_timezone(tz: str) -> bool:
    """Check if timezone string is valid IANA timezone."""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
        return True
    except Exception:
        return False


@router.get("/me", response_model=UserSettingsOut)
async def get_user_settings(user=Depends(get_current_user)) -> UserSettingsOut:
    """Get current user settings (timezone)."""
    user_id = int(user["user_id"])
    settings = await db.get_user_settings(user_id)
    return UserSettingsOut(**settings)


@router.patch("/me", response_model=UserSettingsOut)
async def update_user_settings(
    payload: UserSettingsPatch,
    user=Depends(get_current_user),
) -> UserSettingsOut:
    """Update user settings (timezone)."""
    user_id = int(user["user_id"])
    
    # Validate timezone
    if not _validate_timezone(payload.timezone):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid timezone: {payload.timezone}"
        )
    
    await db.set_user_timezone(user_id, payload.timezone)
    settings = await db.get_user_settings(user_id)
    return UserSettingsOut(**settings)


@router.get("/timezones", response_model=TimezoneListOut)
async def list_timezones() -> TimezoneListOut:
    """Get list of common timezones for UI."""
    return TimezoneListOut(common=COMMON_TIMEZONES)
