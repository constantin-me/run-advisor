import asyncio
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db.models import User
from app.db.session import async_session, get_db
from app.garmin.client import start_login, submit_mfa, token_dir_for
from app.garmin.sync import backfill_history, sync_day

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/garmin")

# Fire-and-forget background tasks need a strong reference or the event loop
# may garbage-collect them mid-run.
_background_tasks: set[asyncio.Task] = set()


class LinkRequest(BaseModel):
    email: str
    password: str


class MfaRequest(BaseModel):
    code: str


class LinkStatus(BaseModel):
    status: str  # "linked" | "mfa_required"


async def _backfill_after_link(telegram_id: int) -> None:
    """Pull the user's Garmin history right after they link, in the background
    so the link request returns immediately instead of blocking for the
    minute-plus a full backfill takes."""
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None or not user.garmin_linked:
            return
        try:
            await backfill_history(db, user)
        except Exception:
            logger.exception("Auto-backfill after link failed for telegram_id=%s", telegram_id)


def _start_backfill(telegram_id: int) -> None:
    task = asyncio.create_task(_backfill_after_link(telegram_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.get("/status", response_model=LinkStatus)
async def status(user: User = Depends(get_current_user)) -> LinkStatus:
    return LinkStatus(status="linked" if user.garmin_linked else "not_linked")


@router.post("/link", response_model=LinkStatus)
async def link(
    body: LinkRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LinkStatus:
    try:
        outcome = await start_login(user.telegram_id, body.email, body.password)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Garmin login failed: {exc}") from exc

    if outcome.status == "linked":
        user.garmin_linked = True
        user.garmin_token_dir = token_dir_for(user.telegram_id)
        await db.commit()
        _start_backfill(user.telegram_id)

    return LinkStatus(status=outcome.status)


@router.post("/mfa", response_model=LinkStatus)
async def mfa(
    body: MfaRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LinkStatus:
    try:
        outcome = await submit_mfa(user.telegram_id, body.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"MFA failed: {exc}") from exc

    user.garmin_linked = True
    user.garmin_token_dir = token_dir_for(user.telegram_id)
    await db.commit()
    _start_backfill(user.telegram_id)
    return LinkStatus(status=outcome.status)


@router.post("/sync")
async def sync(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not user.garmin_linked:
        raise HTTPException(status_code=400, detail="Garmin not linked")

    today = date.today()
    for d in (today, today - timedelta(days=1)):
        await sync_day(db, user, d)

    return {"status": "ok"}
