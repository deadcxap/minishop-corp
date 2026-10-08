"""Scoped callers obtain avatar bytes through Minishop's cache and refresh helper."""

import base64
from datetime import datetime

from aiohttp import web
from bot.app.web.context import get_optional_bot
from bot.app.web.webapp.common import _ensure_cached_telegram_avatar, _telegram_avatar_is_stale
from bot.app.web.webapp.constants import WEBAPP_TELEGRAM_AVATAR_MAX_BYTES
from bot.services.subscription_service import SubscriptionService
from db.dal.user_reads_dal import get_user_by_id
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..member_types import MemberAvatar
from .member_data import ProfileRecord, limiter_for


class CachedAvatar(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    image_bytes: bytes = Field(min_length=1, max_length=WEBAPP_TELEGRAM_AVATAR_MAX_BYTES)
    content_type: str
    updated_at: datetime


def safe_image(data: CachedAvatar) -> bool:
    body = data.image_bytes
    return (
        (data.content_type == "image/jpeg" and body.startswith(b"\xff\xd8\xff"))
        or (data.content_type == "image/png" and body.startswith(b"\x89PNG\r\n\x1a\n"))
        or (data.content_type == "image/webp" and body[:4] == b"RIFF" and body[8:12] == b"WEBP")
    )


class MemberAvatars:
    def __init__(self, service: SubscriptionService) -> None:
        self.limiter = limiter_for(service)

    async def read(
        self,
        request: web.Request,
        session: AsyncSession,
        user_id: int,
        *,
        refresh: bool,
    ) -> MemberAvatar:
        user = await get_user_by_id(session, user_id)
        if user is None:
            return MemberAvatar()
        profile = ProfileRecord.model_validate(user)
        async with self.limiter:
            avatar = await _ensure_cached_telegram_avatar(
                request,
                session,
                user,
                allow_fetch=refresh and get_optional_bot(request) is not None,
            )
        if avatar is None:
            return MemberAvatar(state="missing" if not profile.telegram_id else "unavailable")
        try:
            cached = CachedAvatar.model_validate(avatar)
        except ValidationError:
            return MemberAvatar()
        if not safe_image(cached):
            return MemberAvatar()
        return MemberAvatar(
            data_url=f"data:{cached.content_type};base64,"
            + base64.b64encode(cached.image_bytes).decode("ascii"),
            state="stale" if _telegram_avatar_is_stale(avatar) else "available",
            updated_at=cached.updated_at,
        )
