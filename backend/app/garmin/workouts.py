import asyncio
import logging
from datetime import date

from garminconnect.workout import (
    ConditionType,
    ExecutableStep,
    RunningWorkout,
    StepType,
    TargetType,
    WorkoutSegment,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Activity, PlanWorkout, TrainingPlan, User
from app.garmin.client import get_client

logger = logging.getLogger(__name__)

REST_KEYWORDS = ("rest", "off")

# Map a workout type to an intensity level (1-5). This drives the heart-rate
# target the watch shows during the run — the actually useful metadata, versus
# a bare distance with no effort guidance. Ordered most-specific-first: the
# first keyword found in the (lowercased) type wins.
_ZONE_KEYWORDS: tuple[tuple[int, tuple[str, ...]], ...] = (
    # Unambiguous max-effort words first, so e.g. "VO2 intervals" resolves to
    # Z5 by "vo2" ...
    (5, ("vo2", "v02", "sprint", "speed", "track", "400", "800")),
    # ... but the threshold family is checked before the generic "interval"
    # structure word, so "threshold intervals" correctly resolves to Z4.
    (4, ("tempo", "threshold", "lactate", "cruise", "fartlek")),
    (5, ("interval", "repetition", "rep", "hill")),
    (3, ("steady", "moderate", "marathon", "progression")),
    (2, ("easy", "recovery", "long", "base", "aerobic", "jog", "warm", "cool")),
)

# General aerobic default for an unrecognized type — errs easy so a bad guess
# never pushes someone into a hard session unprompted.
_DEFAULT_ZONE = 2

# Percent-of-max-HR band per intensity level, standard 5-zone %HRmax model.
# Used to turn an intensity level + the athlete's max HR into an explicit
# custom bpm range for the workout step.
_ZONE_HR_PCT: dict[int, tuple[float, float]] = {
    1: (0.50, 0.60),
    2: (0.60, 0.70),
    3: (0.70, 0.80),
    4: (0.80, 0.90),
    5: (0.90, 1.00),
}

# Guard rails on a computed bpm bound, so a bad max-HR estimate can't produce
# an absurd target Garmin would reject.
_MIN_BPM = 60
_MAX_BPM = 220


def is_rest_day(workout_type: str) -> bool:
    return any(kw in workout_type.lower() for kw in REST_KEYWORDS)


def hr_zone_for(workout_type: str) -> int:
    lowered = workout_type.lower()
    for zone, keywords in _ZONE_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return zone
    return _DEFAULT_ZONE


async def estimate_max_hr(db: AsyncSession, user: User) -> int | None:
    """Best available estimate of the athlete's true max HR: the highest max HR
    ever observed across their synced activities. Runners hit at or near their
    max in hard efforts, so the all-time observed peak is a reasonable proxy —
    no age input or Garmin profile call needed. None if we've never recorded a
    heart rate for them."""
    result = await db.execute(
        select(func.max(Activity.max_hr)).where(Activity.user_id == user.id, Activity.max_hr.isnot(None))
    )
    peak = result.scalar_one_or_none()
    return int(peak) if peak else None


def _hr_range(zone: int, max_hr: int) -> tuple[int, int]:
    lo_pct, hi_pct = _ZONE_HR_PCT[zone]
    lo = max(_MIN_BPM, round(lo_pct * max_hr))
    hi = min(_MAX_BPM, round(hi_pct * max_hr))
    return lo, hi


def _build_step(workout_type: str, distance_m: float | None, max_hr: int | None) -> ExecutableStep:
    if distance_m:
        end_condition = {
            "conditionTypeId": ConditionType.DISTANCE,
            "conditionTypeKey": "distance",
            "displayOrder": 3,
            "displayable": True,
        }
        end_value = float(distance_m)
    else:
        # No distance target — fall back to a fixed-time step (30 min) so
        # the workout is still schedulable rather than skipped outright.
        end_condition = {
            "conditionTypeId": ConditionType.TIME,
            "conditionTypeKey": "time",
            "displayOrder": 2,
            "displayable": True,
        }
        end_value = 1800.0

    # Intensity target is heart-rate based (HR is the honest governor for
    # running — pace drifts with terrain, heat, and fatigue). Prefer an
    # explicit custom bpm range derived from the athlete's own max HR, so the
    # limits are their real numbers rather than Garmin's pre-defined zone
    # bands. Only when we can't estimate max HR do we fall back to referencing
    # a configured zone number.
    zone = hr_zone_for(workout_type)
    target_type = {
        "workoutTargetTypeId": TargetType.HEART_RATE_ZONE,
        "workoutTargetTypeKey": "heart.rate.zone",
        "displayOrder": 4,
    }

    extra: dict = {}
    if max_hr is not None:
        lo, hi = _hr_range(zone, max_hr)
        # Custom HR target: raw bpm bounds, no zoneNumber. Garmin's Connect
        # workout API takes targetValueOne/Two as the low/high heart rate.
        extra["targetValueOne"] = lo
        extra["targetValueTwo"] = hi
    else:
        extra["zoneNumber"] = zone

    return ExecutableStep(
        stepOrder=1,
        stepType={"stepTypeId": StepType.INTERVAL, "stepTypeKey": "interval", "displayOrder": 3},
        endCondition=end_condition,
        endConditionValue=end_value,
        targetType=target_type,
        **extra,
    )


def build_running_workout(
    workout_type: str, description: str | None, distance_m, pace_s_per_km, max_hr: int | None = None
) -> RunningWorkout:
    distance = float(distance_m) if distance_m is not None else None
    pace = float(pace_s_per_km) if pace_s_per_km is not None else None
    # Pace no longer drives the step target (heart rate does), but it's still
    # the best estimate we have for the workout's expected duration.
    step = _build_step(workout_type, distance, max_hr)

    if distance and pace:
        duration_s = int(distance / (1000.0 / pace))
    elif distance:
        duration_s = int(distance / 2.8)  # rough easy-pace fallback (~5:00/km)
    else:
        duration_s = 1800

    return RunningWorkout(
        workoutName=workout_type[:50],
        estimatedDurationInSecs=duration_s,
        description=description[:255] if description else None,
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType={"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
                workoutSteps=[step],
            )
        ],
    )


async def _cleanup_superseded_workouts(db: AsyncSession, user: User, client) -> int:
    """When the coach regenerates a plan, the old plan is marked 'superseded'
    but its already-scheduled Garmin workouts were never touched — left as
    orphaned duplicates on the calendar. Remove future-dated ones (past
    dates are left alone as historical record) so a plan update replaces
    calendar entries instead of piling new ones on top."""
    result = await db.execute(
        select(PlanWorkout)
        .join(TrainingPlan, PlanWorkout.plan_id == TrainingPlan.id)
        .where(
            TrainingPlan.user_id == user.id,
            TrainingPlan.status != "active",
            PlanWorkout.garmin_workout_id.isnot(None),
            PlanWorkout.scheduled_date >= date.today(),
        )
    )
    stale = list(result.scalars().all())

    removed = 0
    for w in stale:
        try:
            if w.garmin_scheduled_id:
                try:
                    await asyncio.to_thread(client.unschedule_workout, w.garmin_scheduled_id)
                except Exception:
                    logger.warning("Could not unschedule garmin workout id=%s (may already be gone)", w.id)
            await asyncio.to_thread(client.delete_workout, w.garmin_workout_id)
            w.garmin_workout_id = None
            w.garmin_scheduled_id = None
            removed += 1
        except Exception:
            logger.exception("Failed to clean up stale Garmin workout for plan_workout id=%s", w.id)

    if stale:
        await db.commit()
    return removed


async def sync_plan_to_garmin(db: AsyncSession, user: User) -> dict:
    """Push not-yet-synced workouts from the active plan to Garmin Connect as
    scheduled workouts, so they show up on the paired watch. Idempotent:
    workouts already synced (garmin_workout_id set) are skipped, so calling
    this repeatedly won't create duplicates. Also cleans up stale calendar
    entries left behind by a superseded (regenerated) plan."""
    client = await get_client(user.telegram_id)
    removed_stale = await _cleanup_superseded_workouts(db, user, client)

    result = await db.execute(
        select(TrainingPlan).where(TrainingPlan.user_id == user.id, TrainingPlan.status == "active")
    )
    plan = result.scalars().first()
    if plan is None:
        return {"created": 0, "skipped_rest": 0, "skipped_already_synced": 0, "failed": 0, "removed_stale": removed_stale}

    wk_result = await db.execute(select(PlanWorkout).where(PlanWorkout.plan_id == plan.id))
    workouts = list(wk_result.scalars().all())

    # One estimate for the whole push — every step's custom HR range is scaled
    # off the athlete's max HR.
    max_hr = await estimate_max_hr(db, user)

    created = skipped_rest = skipped_already_synced = failed = 0

    for w in workouts:
        if w.garmin_workout_id:
            skipped_already_synced += 1
            continue
        if is_rest_day(w.workout_type):
            skipped_rest += 1
            continue

        try:
            workout = build_running_workout(
                w.workout_type, w.description, w.target_distance_m, w.target_pace_s_per_km, max_hr=max_hr
            )
            upload_result = await asyncio.to_thread(client.upload_running_workout, workout)
            workout_id = upload_result.get("workoutId")
            if workout_id is None:
                raise ValueError(f"no workoutId in upload response: {upload_result}")

            schedule_result = await asyncio.to_thread(
                client.schedule_workout, workout_id, w.scheduled_date.isoformat()
            )
            scheduled_id = schedule_result.get("workoutScheduleId") or schedule_result.get("id")

            w.garmin_workout_id = str(workout_id)
            w.garmin_scheduled_id = str(scheduled_id) if scheduled_id is not None else None
            created += 1
        except Exception:
            logger.exception("Failed to sync workout id=%s to Garmin", w.id)
            failed += 1

    await db.commit()
    return {
        "created": created,
        "skipped_rest": skipped_rest,
        "skipped_already_synced": skipped_already_synced,
        "failed": failed,
        "removed_stale": removed_stale,
    }
