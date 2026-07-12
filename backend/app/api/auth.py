import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import InvalidInitData, create_session_token, validate_init_data
from app.config import settings
from app.db.models import User
from app.db.session import get_db

router = APIRouter(prefix="/api/auth")

# Real Telegram user ids are always positive, so this can never collide with one.
DEV_TELEGRAM_ID = -1


class SessionRequest(BaseModel):
    init_data: str


class SessionResponse(BaseModel):
    token: str


async def _get_or_create_user(db: AsyncSession, telegram_id: int, username: str | None) -> User:
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, telegram_username=username)
        db.add(user)
        await db.commit()
    elif username and user.telegram_username != username:
        user.telegram_username = username
        await db.commit()
    return user


@router.post("/session", response_model=SessionResponse)
async def create_session(body: SessionRequest, db: AsyncSession = Depends(get_db)) -> SessionResponse:
    try:
        pairs = validate_init_data(body.init_data)
    except InvalidInitData as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    tg_user = json.loads(pairs.get("user", "{}"))
    telegram_id = tg_user.get("id")
    if telegram_id is None:
        raise HTTPException(status_code=400, detail="missing user in initData")

    await _get_or_create_user(db, telegram_id, tg_user.get("username"))
    return SessionResponse(token=create_session_token(telegram_id))


@router.post("/dev-session", response_model=SessionResponse)
async def create_dev_session(db: AsyncSession = Depends(get_db)) -> SessionResponse:
    """No-Telegram local dev login: issues a session for a fixed dev user.
    Disabled unless DEV_MODE=true so it can never ship live by accident."""
    if not settings.dev_mode:
        raise HTTPException(status_code=404, detail="not found")

    await _get_or_create_user(db, DEV_TELEGRAM_ID, "dev")
    return SessionResponse(token=create_session_token(DEV_TELEGRAM_ID))
