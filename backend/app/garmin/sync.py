import asyncio
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from timezonefinder import TimezoneFinder

from app.db.models import Activity, DailyMetric, User
from app.garmin.client import get_client

logger = logging.getLogger(__name__)

# Loads its coordinate-to-timezone shapefile data once at import time and
# reuses it for every lookup — cheap after startup, expensive to recreate.
_tf = TimezoneFinder()


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
            logger.exception(
                "Garmin %s fetch failed for user=%s date=%s", key, user.id, cdate
            )
            raw[key] = None

    stats = raw.get("stats") or {}
    sleep = raw.get("sleep") or {}
    hrv = raw.get("hrv") or {}
    max_metrics = raw.get("max_metrics")
    readiness = raw.get("training_readiness")

    max_metrics_entry = (
        max_metrics[0] if isinstance(max_metrics, list) and max_metrics else max_metrics
    )
    readiness_entry = (
        readiness[0] if isinstance(readiness, list) and readiness else readiness
    )

    values = dict(
        user_id=user.id,
        metric_date=day,
        sleep_score=_dig(sleep, "dailySleepDTO", "sleepScores", "overall", "value"),
        sleep_duration_s=_dig(sleep, "dailySleepDTO", "sleepTimeSeconds"),
        hrv=_dig(hrv, "hrvSummary", "lastNightAvg"),
        resting_hr=_dig(stats, "restingHeartRate"),
        stress_avg=_dig(stats, "averageStressLevel"),
        body_battery=_dig(stats, "bodyBatteryMostRecentValue"),
        vo2max=_dig(max_metrics_entry, "generic", "vo2MaxValue")
        or _dig(max_metrics_entry, "vo2MaxValue"),
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
        logger.exception(
            "Garmin activities fetch failed for user=%s date=%s", user.id, cdate
        )
        activities_raw = None

    activities = _dig(activities_raw, "ActivitiesForDay", "payload", default=[]) or []
    for activity in activities:
        row = await _upsert_activity(db, user, activity)
        if row is None:
            continue
        await _ensure_activity_location(db, client, row)
        _maybe_bootstrap_user_location(user, row)

    await db.commit()


def _extract_location(activity: dict) -> dict[str, Any] | None:
    """GPS fields are present directly on activity dicts from the bulk
    get_activities() endpoint (used by sync_recent_activities) but never on
    the reduced get_activities_fordate() payload (used by sync_day) — so
    this is a free check either way, and callers fall back to fetching
    get_activity() only when it comes up empty."""
    lat, lon = activity.get("startLatitude"), activity.get("startLongitude")
    if lat is None or lon is None:
        return None
    return {
        "latitude": lat,
        "longitude": lon,
        "location_name": activity.get("locationName") or None,
    }


async def _ensure_activity_location(
    db: AsyncSession, client: Any, activity_row: Activity
) -> None:
    """Backfill this activity's own location if it wasn't already resolved
    at upsert time (true for every sync_day-sourced activity, since that
    endpoint never includes GPS). Checked exactly once per activity via
    location_checked — on success or confirmed-indoor-absence it's marked
    checked so it's never queried again; on a transient API failure it's
    left unchecked so a later sync retries it."""
    if activity_row.location_checked:
        return

    try:
        detail = await asyncio.to_thread(
            client.get_activity, activity_row.garmin_activity_id
        )
    except Exception:
        logger.exception(
            "get_activity failed for activity=%s", activity_row.garmin_activity_id
        )
        return

    location = _extract_location(detail)
    if location is not None:
        activity_row.latitude = location["latitude"]
        activity_row.longitude = location["longitude"]
        activity_row.location_name = location["location_name"]
    activity_row.location_checked = True


def _maybe_bootstrap_user_location(user: User, activity_row: Activity) -> None:
    """Auto-set the user's home location (used for weather forecasts) from
    the first outdoor activity we find location data for. Only runs while
    no location is set yet, making it a permanent no-op once resolved.
    Users whose activities are all indoor (treadmill, pool) never trigger
    this, which is intentional: weather genuinely doesn't matter to them."""
    if user.latitude is not None or activity_row.latitude is None:
        return

    user.latitude = activity_row.latitude
    user.longitude = activity_row.longitude
    user.location_name = activity_row.location_name
    user.timezone = _tf.timezone_at(
        lat=float(activity_row.latitude), lng=float(activity_row.longitude)
    )


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

    values = dict(
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

    # Only the bulk get_activities() endpoint includes GPS inline; when it
    # does, resolve location for free here so sync_recent_activities never
    # needs the extra per-activity get_activity() call at all.
    location = _extract_location(activity)
    if location is not None:
        values["latitude"] = location["latitude"]
        values["longitude"] = location["longitude"]
        values["location_name"] = location["location_name"]
        values["location_checked"] = True

    return values


async def _upsert_activity(
    db: AsyncSession, user: User, activity: dict[str, Any]
) -> Activity | None:
    act_values = _activity_values(user, activity)
    if act_values is None:
        return None

    act_stmt = insert(Activity).values(**act_values)
    act_stmt = act_stmt.on_conflict_do_update(
        index_elements=[Activity.user_id, Activity.garmin_activity_id],
        # Only columns present in act_values are touched on conflict — in
        # particular, when this upsert has no inline location data,
        # location_checked/latitude/longitude/location_name are simply
        # absent here and so stay untouched, rather than being reset.
        set_={
            k: v
            for k, v in act_values.items()
            if k not in ("user_id", "garmin_activity_id")
        },
    ).returning(Activity)
    result = await db.execute(act_stmt)
    return result.scalar_one()


async def sync_recent_activities(db: AsyncSession, user: User, limit: int = 200) -> int:
    """Bulk-backfill activity history in one Garmin API call (much faster
    than iterating sync_day per date). Returns the number of activities
    upserted."""
    client = await get_client(user.telegram_id)

    try:
        activities = await asyncio.to_thread(client.get_activities, 0, limit)
    except Exception:
        logger.exception(
            "Garmin bulk activities fetch failed for user=%s limit=%d", user.id, limit
        )
        return 0

    if not isinstance(activities, list):
        logger.warning(
            "Unexpected get_activities response shape for user=%s: %s",
            user.id,
            type(activities),
        )
        return 0

    count = 0
    for activity in activities:
        row = await _upsert_activity(db, user, activity)
        if row is None:
            continue
        count += 1
        # Inline GPS (if any) was already resolved in _activity_values, so
        # this is a no-op for every activity here in practice — kept for
        # consistency with sync_day, and as a safety net if Garmin ever
        # omits GPS from this endpoint for a specific activity.
        await _ensure_activity_location(db, client, row)
        _maybe_bootstrap_user_location(user, row)

    await db.commit()
    return count
