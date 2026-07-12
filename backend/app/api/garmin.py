from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.garmin.client import start_login, submit_mfa, token_dir_for
from app.garmin.sync import sync_day, sync_recent_activities

router = APIRouter(prefix="/api/garmin")


class LinkRequest(BaseModel):
    email: str
    password: str


class MfaRequest(BaseModel):
    code: str


class LinkStatus(BaseModel):
    status: str  # "linked" | "mfa_required"


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


class BackfillRequest(BaseModel):
    days: int = 30


class BackfillResult(BaseModel):
    status: str
    activities_synced: int
    metrics_days_synced: int


@router.post("/backfill", response_model=BackfillResult)
async def backfill(
    body: BackfillRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BackfillResult:
    """Pulls as much history as practical in one go: up to 200 recent
    activities in a single bulk call, plus daily metrics for the last
    `days` days (capped at a year — each day is its own set of Garmin
    calls, so this is much slower than /sync)."""
    if not user.garmin_linked:
        raise HTTPException(status_code=400, detail="Garmin not linked")

    days = max(1, min(body.days, 365))

    activities_synced = await sync_recent_activities(db, user, limit=200)

    today = date.today()
    for i in range(days):
        await sync_day(db, user, today - timedelta(days=i))

    return BackfillResult(status="ok", activities_synced=activities_synced, metrics_days_synced=days)
