import asyncio
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Activity, DailyMetric, User
from app.garmin.client import get_client

logger = logging.getLogger(__name__)


def _dig(d: Any, *path: str, default: Any = None) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def _parse_garmin_datetime(raw: str | None) -> datetime | None:
    """Garmin's activity endpoints don't agree on a timestamp format:
    get_activities_fordate uses "2026-06-09 05:00:49", get_activities uses
    ISO "2026-06-21T12:10:13.0". Try both rather than assuming one."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("Unrecognized Garmin timestamp format: %r", raw)
        return None


async def sync_day(db: AsyncSession, user: User, day: date) -> None:
    """Pull one day of Garmin data for `user` and upsert it. Any single
    metric source failing (Garmin's field layout is not a stable public
    contract) is swallowed and logged rather than aborting the whole sync."""
    client = await get_client(user.telegram_id)
    cdate = day.isoformat()

    raw: dict[str, Any] = {}
    for key, fn in (
        ("stats", client.get_stats),
        ("sleep", client.get_sleep_data),
        ("hrv", client.get_hrv_data),
        ("max_metrics", client.get_max_metrics),
        ("training_readiness", client.get_training_readiness),
    ):
        try:
            raw[key] = await asyncio.to_thread(fn, cdate)
        except Exception:
            logger.exception("Garmin %s fetch failed for user=%s date=%s", key, user.id, cdate)
            raw[key] = None

    stats = raw.get("stats") or {}
    sleep = raw.get("sleep") or {}
    hrv = raw.get("hrv") or {}
    max_metrics = raw.get("max_metrics")
    readiness = raw.get("training_readiness")

    max_metrics_entry = max_metrics[0] if isinstance(max_metrics, list) and max_metrics else max_metrics
    readiness_entry = readiness[0] if isinstance(readiness, list) and readiness else readiness

    values = dict(
        user_id=user.id,
        metric_date=day,
        sleep_score=_dig(sleep, "dailySleepDTO", "sleepScores", "overall", "value"),
        sleep_duration_s=_dig(sleep, "dailySleepDTO", "sleepTimeSeconds"),
        hrv=_dig(hrv, "hrvSummary", "lastNightAvg"),
        resting_hr=_dig(stats, "restingHeartRate"),
        stress_avg=_dig(stats, "averageStressLevel"),
        body_battery=_dig(stats, "bodyBatteryMostRecentValue"),
        vo2max=_dig(max_metrics_entry, "generic", "vo2MaxValue") or _dig(max_metrics_entry, "vo2MaxValue"),
        training_readiness=_dig(readiness_entry, "score"),
        raw=raw,
    )

    stmt = insert(DailyMetric).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[DailyMetric.user_id, DailyMetric.metric_date],
        set_={k: v for k, v in values.items() if k not in ("user_id", "metric_date")},
    )
    await db.execute(stmt)

    try:
        activities_raw = await asyncio.to_thread(client.get_activities_fordate, cdate)
    except Exception:
        logger.exception("Garmin activities fetch failed for user=%s date=%s", user.id, cdate)
        activities_raw = None

    for activity in _dig(activities_raw, "ActivitiesForDay", "payload", default=[]) or []:
        await _upsert_activity(db, user, activity)

    await db.commit()


def _activity_values(user: User, activity: dict[str, Any]) -> dict[str, Any] | None:
    activity_id = activity.get("activityId")
    if activity_id is None:
        return None

    distance_m = activity.get("distance")
    duration_s = activity.get("duration")
    avg_speed = activity.get("averageSpeed")
    if avg_speed:
        pace = 1000 / avg_speed
    elif distance_m and duration_s:
        pace = duration_s / (distance_m / 1000)
    else:
        pace = None

    start_time = _parse_garmin_datetime(activity.get("startTimeLocal"))

    return dict(
        user_id=user.id,
        garmin_activity_id=str(activity_id),
        activity_type=_dig(activity, "activityType", "typeKey"),
        start_time=start_time,
        distance_m=distance_m,
        duration_s=duration_s,
        avg_pace_s_per_km=pace,
        avg_hr=activity.get("averageHR"),
        max_hr=activity.get("maxHR"),
        cadence=activity.get("averageRunningCadenceInStepsPerMinute"),
        raw=activity,
    )


async def _upsert_activity(db: AsyncSession, user: User, activity: dict[str, Any]) -> None:
    act_values = _activity_values(user, activity)
    if act_values is None:
        return

    act_stmt = insert(Activity).values(**act_values)
    act_stmt = act_stmt.on_conflict_do_update(
        index_elements=[Activity.user_id, Activity.garmin_activity_id],
        set_={k: v for k, v in act_values.items() if k not in ("user_id", "garmin_activity_id")},
    )
    await db.execute(act_stmt)


async def sync_recent_activities(db: AsyncSession, user: User, limit: int = 200) -> int:
    """Bulk-backfill activity history in one Garmin API call (much faster
    than iterating sync_day per date). Returns the number of activities
    upserted."""
    client = await get_client(user.telegram_id)

    try:
        activities = await asyncio.to_thread(client.get_activities, 0, limit)
    except Exception:
        logger.exception("Garmin bulk activities fetch failed for user=%s limit=%d", user.id, limit)
        return 0

    if not isinstance(activities, list):
        logger.warning("Unexpected get_activities response shape for user=%s: %s", user.id, type(activities))
        return 0

    count = 0
    for activity in activities:
        await _upsert_activity(db, user, activity)
        count += 1

    await db.commit()
    return count
