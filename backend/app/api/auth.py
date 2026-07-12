import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    InvalidInitData,
    create_session_token,
    get_current_user,
    validate_init_data,
    validate_login_widget_data,
)
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


class LoginWidgetRequest(BaseModel):
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int
    hash: str


class AuthConfig(BaseModel):
    bot_username: str


class WhoAmI(BaseModel):
    telegram_id: int
    telegram_username: str | None


async def _get_or_create_user(db: AsyncSession, telegram_id: int, username: str | None) -> User:
    """Atomic upsert — SELECT-then-INSERT has a real TOCTOU race under
    concurrent/duplicate requests (hit in practice: a double-fired frontend
    effect caused two near-simultaneous calls, and the second's INSERT hit
    the unique constraint and 500'd). A single INSERT ... ON CONFLICT is
    race-safe at the DB level regardless of what the caller does."""
    insert_stmt = pg_insert(User).values(telegram_id=telegram_id, telegram_username=username)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=[User.telegram_id],
        set_={"telegram_username": func.coalesce(insert_stmt.excluded.telegram_username, User.telegram_username)},
    ).returning(User)
    result = await db.execute(stmt)
    user = result.scalar_one()
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


@router.get("/whoami", response_model=WhoAmI)
async def whoami(user: User = Depends(get_current_user)) -> WhoAmI:
    """Lightweight token-validity check — a stored web session token is
    verified against this before being trusted on page load, since a
    401 here means the frontend should fall back to a fresh login."""
    return WhoAmI(telegram_id=user.telegram_id, telegram_username=user.telegram_username)


@router.get("/config", response_model=AuthConfig)
async def get_auth_config() -> AuthConfig:
    from app.telegram.bot import get_bot_username

    return AuthConfig(bot_username=await get_bot_username())


@router.post("/telegram-login", response_model=SessionResponse)
async def create_login_widget_session(
    body: LoginWidgetRequest, db: AsyncSession = Depends(get_db)
) -> SessionResponse:
    """Session login for the plain-browser path (Telegram Login Widget),
    used when the app isn't opened as a Telegram Mini App. Maps to the same
    User row (by telegram_id) as the Mini App flow — same Garmin link, same
    chat history, same everything, just a different entry point."""
    try:
        validate_login_widget_data(body.model_dump(exclude_none=True))
    except InvalidInitData as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    await _get_or_create_user(db, body.id, body.username)
    return SessionResponse(token=create_session_token(body.id))


@router.post("/dev-session", response_model=SessionResponse)
async def create_dev_session(db: AsyncSession = Depends(get_db)) -> SessionResponse:
    """No-Telegram local dev login: issues a session for a fixed dev user.
    Disabled unless DEV_MODE=true so it can never ship live by accident."""
    if not settings.dev_mode:
        raise HTTPException(status_code=404, detail="not found")

    await _get_or_create_user(db, DEV_TELEGRAM_ID, "dev")
    return SessionResponse(token=create_session_token(DEV_TELEGRAM_ID))
