from datetime import date, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db.models import Activity, DailyMetric, User
from app.db.session import get_db

router = APIRouter(prefix="/api")


class DailyMetricOut(BaseModel):
    metric_date: date
    sleep_score: int | None
    sleep_duration_s: float | None
    hrv: float | None
    resting_hr: int | None
    stress_avg: int | None
    body_battery: int | None
    vo2max: float | None
    training_readiness: int | None

    model_config = {"from_attributes": True}


class ActivityOut(BaseModel):
    garmin_activity_id: str
    activity_type: str | None
    start_time: object | None
    distance_m: float | None
    duration_s: float | None
    avg_pace_s_per_km: float | None
    avg_hr: int | None

    model_config = {"from_attributes": True}


@router.get("/metrics", response_model=list[DailyMetricOut])
async def get_metrics(
    days: int = 7,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DailyMetric]:
    since = date.today() - timedelta(days=days)
    result = await db.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user.id, DailyMetric.metric_date >= since)
        .order_by(DailyMetric.metric_date.desc())
    )
    return list(result.scalars().all())


@router.get("/activities", response_model=list[ActivityOut])
async def get_activities(
    limit: int = 10,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Activity]:
    result = await db.execute(
        select(Activity)
        .where(Activity.user_id == user.id)
        .order_by(Activity.start_time.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
