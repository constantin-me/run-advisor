import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.garmin.client import get_client

logger = logging.getLogger(__name__)

_HR_ZONES_URL = "/biometric-service/heartRateZones"


def _dig(d: Any, *path: str, default: Any = None) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def _extract_hr_zones(rows: Any) -> dict[str, Any] | None:
    """Pick the running/default HR-zone config and pull max HR + the five zone
    floors. Garmin returns one row per sport; DEFAULT applies to running when
    there's no running-specific override."""
    if not isinstance(rows, list) or not rows:
        return None
    row = next((r for r in rows if r.get("sport") == "DEFAULT"), rows[0])
    floors = [row.get(f"zone{i}Floor") for i in range(1, 6)]
    if any(f is None for f in floors):
        return None
    return {
        "max_hr": row.get("maxHeartRateUsed"),
        "resting_hr": row.get("restingHeartRateUsed"),
        "lactate_threshold_hr": row.get("lactateThresholdHeartRateUsed"),
        "zone_floors": [int(f) for f in floors],
        "training_method": row.get("trainingMethod"),
    }


def _fetch_snapshot(client: Any) -> dict[str, Any]:
    """Synchronous Garmin profile pull, run in a worker thread. Every sub-call
    is independent and swallowed on failure — Garmin's field layout isn't a
    stable contract, and a partial profile is far more useful than none."""
    today = date.today().isoformat()

    def _safe(fn, *args):
        try:
            return fn(*args)
        except Exception:
            logger.exception("Garmin profile sub-fetch %s failed", getattr(fn, "__name__", fn))
            return None

    profile = _safe(client.get_user_profile) or {}
    user_data = profile.get("userData") or {}

    zones = _extract_hr_zones(_safe(client.connectapi, _HR_ZONES_URL))
    races = _safe(client.get_race_predictions) or {}
    fitness = _safe(client.get_fitnessage_data, today) or {}

    weight_g = user_data.get("weight")

    # Deliberately not stored: BMI and Garmin's fitness age. BMI is meaningless
    # without body composition (muscle vs fat), and fitness age is largely
    # BMI-driven — neither should shape weight or improvement advice. Height is
    # dropped too so BMI can't be re-derived downstream.
    return {
        "weight_kg": round(weight_g / 1000, 1) if weight_g else None,
        "gender": user_data.get("gender"),
        "vo2max": user_data.get("vo2MaxRunning"),
        "lactate_threshold_hr": user_data.get("lactateThresholdHeartRate"),
        "lactate_threshold_speed": user_data.get("lactateThresholdSpeed"),
        "hr": zones,
        "race_predictions_s": {
            "5k": races.get("time5K"),
            "10k": races.get("time10K"),
            "half": races.get("timeHalfMarathon"),
            "marathon": races.get("timeMarathon"),
        }
        if isinstance(races, dict) and races
        else None,
        "chronological_age": fitness.get("chronologicalAge"),
        "resting_hr": _dig(fitness, "components", "rhr", "value"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def refresh_profile(db: AsyncSession, user: User) -> bool:
    """Fetch the athlete's Garmin profile and store the normalized snapshot on
    the user. Returns True on success. Failure-safe: a Garmin outage leaves the
    previous snapshot intact rather than blowing up the caller."""
    if not user.garmin_linked:
        return False

    try:
        client = await get_client(user.telegram_id)
        snapshot = await asyncio.to_thread(_fetch_snapshot, client)
    except Exception:
        logger.exception("Profile refresh failed for user=%s", user.id)
        return False

    user.garmin_profile = snapshot
    user.profile_synced_at = datetime.now(timezone.utc)
    await db.commit()
    return True
