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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PlanWorkout, TrainingPlan, User
from app.garmin.client import get_client

logger = logging.getLogger(__name__)

REST_KEYWORDS = ("rest", "off")


def is_rest_day(workout_type: str) -> bool:
    return any(kw in workout_type.lower() for kw in REST_KEYWORDS)


def _build_step(distance_m: float | None, pace_s_per_km: float | None) -> ExecutableStep:
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

    extra: dict = {}
    if pace_s_per_km:
        speed = 1000.0 / float(pace_s_per_km)
        target_type = {
            "workoutTargetTypeId": TargetType.PACE_ZONE,
            "workoutTargetTypeKey": "pace.zone",
            "displayOrder": 6,
        }
        # +/-5% tolerance band around the target pace, expressed as speed (m/s)
        # per Garmin's pace-zone target convention.
        extra["targetValueOne"] = speed * 0.95
        extra["targetValueTwo"] = speed * 1.05
    else:
        target_type = {
            "workoutTargetTypeId": TargetType.NO_TARGET,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1,
        }

    return ExecutableStep(
        stepOrder=1,
        stepType={"stepTypeId": StepType.INTERVAL, "stepTypeKey": "interval", "displayOrder": 3},
        endCondition=end_condition,
        endConditionValue=end_value,
        targetType=target_type,
        **extra,
    )


def build_running_workout(workout_type: str, description: str | None, distance_m, pace_s_per_km) -> RunningWorkout:
    distance = float(distance_m) if distance_m is not None else None
    pace = float(pace_s_per_km) if pace_s_per_km is not None else None
    step = _build_step(distance, pace)

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

    created = skipped_rest = skipped_already_synced = failed = 0

    for w in workouts:
        if w.garmin_workout_id:
            skipped_already_synced += 1
            continue
        if is_rest_day(w.workout_type):
            skipped_rest += 1
            continue

        try:
            workout = build_running_workout(w.workout_type, w.description, w.target_distance_m, w.target_pace_s_per_km)
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
