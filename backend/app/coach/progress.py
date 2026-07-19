from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Activity, DailyMetric, User

# Length of each comparison window. The evaluation contrasts the most recent
# PERIOD_DAYS against the PERIOD_DAYS before it.
PERIOD_DAYS = 14


@dataclass
class PeriodStats:
    runs: int
    distance_km: float
    avg_pace_s_per_km: float | None
    avg_hr: int | None
    avg_resting_hr: float | None
    vo2max: float | None


async def _period_stats(db: AsyncSession, user: User, lo: date, hi: date) -> PeriodStats:
    """Aggregate one window [lo, hi) of the user's runs + daily metrics."""
    lo_dt = datetime.combine(lo, time.min, tzinfo=timezone.utc)
    hi_dt = datetime.combine(hi, time.min, tzinfo=timezone.utc)

    run_row = (
        await db.execute(
            select(
                func.count(Activity.id),
                func.coalesce(func.sum(Activity.distance_m), 0),
                func.avg(Activity.avg_pace_s_per_km),
                func.avg(Activity.avg_hr),
            ).where(
                Activity.user_id == user.id,
                Activity.activity_type.ilike("%run%"),
                Activity.start_time >= lo_dt,
                Activity.start_time < hi_dt,
            )
        )
    ).one()
    runs, distance_m, avg_pace, avg_hr = run_row

    avg_rhr = (
        await db.execute(
            select(func.avg(DailyMetric.resting_hr)).where(
                DailyMetric.user_id == user.id,
                DailyMetric.metric_date >= lo,
                DailyMetric.metric_date < hi,
                DailyMetric.resting_hr.isnot(None),
            )
        )
    ).scalar_one_or_none()

    vo2 = (
        await db.execute(
            select(DailyMetric.vo2max)
            .where(
                DailyMetric.user_id == user.id,
                DailyMetric.metric_date >= lo,
                DailyMetric.metric_date < hi,
                DailyMetric.vo2max.isnot(None),
            )
            .order_by(DailyMetric.metric_date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return PeriodStats(
        runs=int(runs or 0),
        distance_km=round(float(distance_m or 0) / 1000, 1),
        avg_pace_s_per_km=round(float(avg_pace), 1) if avg_pace is not None else None,
        avg_hr=round(float(avg_hr)) if avg_hr is not None else None,
        avg_resting_hr=round(float(avg_rhr), 1) if avg_rhr is not None else None,
        vo2max=round(float(vo2), 1) if vo2 is not None else None,
    )


def _delta(cur, prior):
    if cur is None or prior is None:
        return None
    return round(cur - prior, 1)


async def compute_progress(db: AsyncSession, user: User) -> dict | None:
    """Deterministic bi-weekly progress snapshot: the most recent PERIOD_DAYS
    vs the prior PERIOD_DAYS across volume, pace, HR, resting HR, and VO2max,
    plus the athlete's current race predictions. Returns None when there's not
    enough recent running to say anything useful."""
    today = date.today()
    cur = await _period_stats(db, user, today - timedelta(days=PERIOD_DAYS), today)
    if cur.runs == 0:
        return None
    prior = await _period_stats(db, user, today - timedelta(days=2 * PERIOD_DAYS), today - timedelta(days=PERIOD_DAYS))

    deltas = {
        "distance_km": _delta(cur.distance_km, prior.distance_km),
        # negative pace delta = faster; negative HR/RHR = lower (fitter)
        "avg_pace_s_per_km": _delta(cur.avg_pace_s_per_km, prior.avg_pace_s_per_km),
        "avg_hr": _delta(cur.avg_hr, prior.avg_hr),
        "avg_resting_hr": _delta(cur.avg_resting_hr, prior.avg_resting_hr),
        "vo2max": _delta(cur.vo2max, prior.vo2max),
    }

    return {
        "period_days": PERIOD_DAYS,
        "current": asdict(cur),
        "prior": asdict(prior),
        "deltas": deltas,
        "race_predictions_s": (user.garmin_profile or {}).get("race_predictions_s"),
    }
