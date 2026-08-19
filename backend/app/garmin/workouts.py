import asyncio
import logging
from datetime import date
from functools import lru_cache

from garminconnect import exercises as exercise_catalog
from garminconnect.workout import (
    ConditionType,
    ExecutableStep,
    RepeatGroup,
    StepType,
    TargetType,
    WorkoutSegment,
    create_strength_exercise_step,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Activity, PlanWorkout, TrainingPlan, User
from app.garmin.client import get_client
from app.garmin.sports import Sport, get_sport, upload_callable

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

# --- Structured (multi-step) workouts ------------------------------------
#
# A workout can carry an explicit step breakdown so a session like
# "3 x 200m sprint / 200m jog" survives all the way to the watch instead of
# collapsing into one flat block. Schema (stored on PlanWorkout.steps):
#
#   [
#     {"kind": "warmup", "duration_s": 600, "intensity": "easy"},
#     {"repeat": 3, "steps": [
#        {"kind": "run",      "distance_m": 200, "intensity": "sprint"},
#        {"kind": "recovery", "distance_m": 200, "intensity": "easy"}
#     ]},
#     {"kind": "cooldown", "duration_s": 600, "intensity": "easy"}
#   ]
#
# Each leaf step has exactly one end condition: distance_m, duration_s, or
# lap_button. Repeats nest one level (enough for running).

# Intensity word -> the same 1-5 intensity level the keyword matcher produces,
# so both paths feed the identical HR-range resolution.
_INTENSITY_ZONE: dict[str, int] = {
    "rest": 1,
    "recovery": 1,
    "easy": 2,
    "jog": 2,
    "aerobic": 2,
    "steady": 3,
    "moderate": 3,
    "marathon": 3,
    "tempo": 4,
    "threshold": 4,
    "interval": 5,
    "hard": 5,
    "sprint": 5,
    "max": 5,
}

# kind -> Garmin step type. The keys are what the coach emits.
_STEP_KINDS: dict[str, tuple[int, str, int]] = {
    "warmup": (StepType.WARMUP, "warmup", 1),
    "cooldown": (StepType.COOLDOWN, "cooldown", 2),
    "run": (StepType.INTERVAL, "interval", 3),
    "interval": (StepType.INTERVAL, "interval", 3),
    "recovery": (StepType.RECOVERY, "recovery", 4),
    "rest": (StepType.REST, "rest", 5),
}

# Fallback category when an exercise name can't be matched in Garmin's catalog.
# The workout still runs — the watch just shows the generic category, and the
# requested name is preserved in the step description.
_FALLBACK_CATEGORY = "TOTAL_BODY"

# Seconds per repetition, for estimating how long a strength block takes.
_SECONDS_PER_REP = 3.0

# Rough speed (m/s) per intensity level, used only to estimate how long a
# distance-based step takes so estimatedDurationInSecs is sane. Scaled by the
# athlete's 5K prediction when we have one.
_INTENSITY_SPEED: dict[int, float] = {1: 2.2, 2: 2.6, 3: 3.0, 4: 3.4, 5: 4.2}

# Speed the _INTENSITY_SPEED table is written against (a ~3.0 m/s ≈ 5:33/km
# steady runner), so a faster or slower athlete scales the whole table.
_REFERENCE_STEADY_SPEED = 3.0


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


def _hr_range_from_pct(zone: int, max_hr: int) -> tuple[int, int]:
    lo_pct, hi_pct = _ZONE_HR_PCT[zone]
    lo = max(_MIN_BPM, round(lo_pct * max_hr))
    hi = min(_MAX_BPM, round(hi_pct * max_hr))
    return lo, hi


def _hr_range_from_zones(zone: int, zone_floors: list[int], max_hr: int) -> tuple[int, int]:
    """Turn an intensity level into a bpm range using the athlete's own
    configured Garmin zone floors — their real boundaries, not a generic
    %-of-max estimate. zone_floors is [z1..z5] floor bpm; a zone spans from its
    own floor up to the next zone's floor (or max HR for zone 5)."""
    lo = zone_floors[zone - 1]
    hi = zone_floors[zone] if zone < 5 else max_hr
    lo = max(_MIN_BPM, int(lo))
    hi = min(_MAX_BPM, int(hi))
    return lo, hi


def _resolve_hr_range(zone: int, profile: dict | None, max_hr_est: int | None) -> tuple[int, int] | None:
    """Best available custom bpm range for the intensity level:
    1. the athlete's configured Garmin zone floors (exact, personal),
    2. else a %-of-max band off an estimated max HR,
    3. else None (caller falls back to a zone-number reference)."""
    hr = (profile or {}).get("hr") or {}
    floors = hr.get("zone_floors")
    zmax = hr.get("max_hr")
    if isinstance(floors, list) and len(floors) == 5 and zmax:
        return _hr_range_from_zones(zone, floors, int(zmax))
    if max_hr_est:
        return _hr_range_from_pct(zone, max_hr_est)
    return None


def _build_step(
    workout_type: str,
    distance_m: float | None,
    profile: dict | None,
    max_hr_est: int | None,
    sport: Sport | None = None,
) -> ExecutableStep:
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
    # explicit custom bpm range from the athlete's own configured Garmin zones
    # so the limits are their real numbers; fall back to a %-of-max estimate,
    # then to referencing a zone number if we have no HR data at all.
    zone = hr_zone_for(workout_type)
    extra = _target_for(zone, profile, max_hr_est, no_target=False, sport=sport)

    return ExecutableStep(
        stepOrder=1,
        stepType={"stepTypeId": StepType.INTERVAL, "stepTypeKey": "interval", "displayOrder": 3},
        endCondition=end_condition,
        endConditionValue=end_value,
        **extra,
    )


def _intensity_zone(step: dict) -> int:
    """Intensity level (1-5) for a structured step. Falls back to the step kind
    when no explicit intensity is given, so a bare {"kind": "warmup"} still
    gets a sensible easy target."""
    raw = (step.get("intensity") or step.get("kind") or "").strip().lower()
    if raw in _INTENSITY_ZONE:
        return _INTENSITY_ZONE[raw]
    return _DEFAULT_ZONE


def _end_condition(step: dict) -> tuple[dict, float | None]:
    """Exactly one end condition per step, in priority order. A step with none
    of them falls back to the lap button so it is still executable on the watch
    rather than being rejected outright."""
    distance = step.get("distance_m")
    duration = step.get("duration_s")
    if distance:
        return (
            {
                "conditionTypeId": ConditionType.DISTANCE,
                "conditionTypeKey": "distance",
                "displayOrder": 3,
                "displayable": True,
            },
            float(distance),
        )
    if duration:
        return (
            {
                "conditionTypeId": ConditionType.TIME,
                "conditionTypeKey": "time",
                "displayOrder": 2,
                "displayable": True,
            },
            float(duration),
        )
    return (
        {
            "conditionTypeId": ConditionType.LAP_BUTTON,
            "conditionTypeKey": "lap.button",
            "displayOrder": 1,
            "displayable": True,
        },
        None,
    )


def _target_for(
    zone: int,
    profile: dict | None,
    max_hr_est: int | None,
    *,
    no_target: bool,
    sport: Sport | None = None,
) -> dict:
    """Target block plus the bpm bounds, as kwargs for ExecutableStep.

    Only sports where heart rate is the honest governor get an HR target. A
    lifting set, a yoga hold or an in-water interval get none — a bpm range
    there is either meaningless or actively wrong (HR straps read badly in
    water, and load, not heart rate, drives a strength set)."""
    if no_target or (sport is not None and sport.target != "hr"):
        return {
            "targetType": {
                "workoutTargetTypeId": TargetType.NO_TARGET,
                "workoutTargetTypeKey": "no.target",
                "displayOrder": 1,
            }
        }

    out: dict = {
        "targetType": {
            "workoutTargetTypeId": TargetType.HEART_RATE_ZONE,
            "workoutTargetTypeKey": "heart.rate.zone",
            "displayOrder": 4,
        }
    }
    hr_range = _resolve_hr_range(zone, profile, max_hr_est)
    if hr_range is not None:
        # Verified against a real workout fetched back from Garmin Connect:
        # targetValueOne/Two are raw bpm bounds, not offsets.
        out["targetValueOne"], out["targetValueTwo"] = hr_range
    else:
        out["zoneNumber"] = zone
    return out


def _normalize_exercise(name: str) -> str:
    """Fold an exercise name to a comparable form.

    Garmin's catalog hyphenates and singularizes inconsistently ("Pull-up",
    "Banded Push-ups"), and a coach writes "pull ups". Punctuation and trailing
    plurals are the only differences that matter, so strip both."""
    words = "".join(ch if ch.isalnum() else " " for ch in name.lower()).split()
    # Drop a trailing plural "s", but never off a word that ends in "ss"
    # ("press", "cross") where the s is part of the word.
    return " ".join(
        w[:-1] if len(w) > 2 and w.endswith("s") and not w.endswith("ss") else w for w in words
    )


@lru_cache(maxsize=1)
def _normalized_catalog() -> list[tuple[str, dict]]:
    """(normalized name, entry) for the whole catalog, shortest name first so an
    ambiguous match resolves to the least-qualified variant."""
    entries = [(_normalize_exercise(e["name"]), e) for e in exercise_catalog.EXERCISES]
    return sorted(entries, key=lambda pair: len(pair[0]))


def _best_containing(catalog: list[tuple[str, dict]], needle: str) -> dict | None:
    """Shortest catalog entry containing `needle`, preferring one that ends on
    the same movement word. "barbell squat" is inside "barbell squat clean",
    but a squat is what was asked for — the trailing word is what names the
    movement, so a candidate ending in it wins."""
    if not needle:
        return None
    matches = [e for norm, e in catalog if needle in norm]
    if not matches:
        return None
    last_word = needle.split()[-1]
    same_movement = [e for e in matches if _normalize_exercise(e["name"]).endswith(last_word)]
    return (same_movement or matches)[0]


def _resolve_exercise(name: str) -> tuple[str, str]:
    """Map a human exercise name to Garmin's (category, exerciseName) pair.

    The coach writes display names ("Barbell Bench Press"); Garmin wants enum
    values from its own 1500-entry catalog. Exact match first, then a
    normalized exact match, then a normalized substring search, then the last
    two words alone (equipment qualifiers like "cable" are what usually miss).
    Anything still unmatched degrades to a generic category rather than failing
    the whole workout — the athlete keeps the session, just with a less
    specific label on the watch."""
    raw = (name or "").strip()
    if not raw:
        return _FALLBACK_CATEGORY, ""

    entry = exercise_catalog.resolve(raw)
    if entry is None:
        needle = _normalize_exercise(raw)
        catalog = _normalized_catalog()
        entry = next((e for norm, e in catalog if norm == needle), None)
        if entry is None:
            # Compound words split differently on each side ("Lat Pulldown" vs
            # Garmin's "Lat Pull-down"), so compare with spacing removed too.
            squashed = needle.replace(" ", "")
            entry = next((e for norm, e in catalog if norm.replace(" ", "") == squashed), None)
        if entry is None:
            entry = _best_containing(catalog, needle)
        if entry is None:
            words = needle.split()
            if len(words) > 2:
                entry = _best_containing(catalog, " ".join(words[-2:]))

    if entry is None:
        logger.warning("Unknown exercise %r — falling back to %s", raw, _FALLBACK_CATEGORY)
        return _FALLBACK_CATEGORY, ""
    return entry["category"], entry.get("exercise") or ""


def _build_exercise_step(step: dict, order: int, child_step_id: int | None) -> ExecutableStep:
    """One rep-based strength step (an exercise inside a set)."""
    category, exercise_name = _resolve_exercise(str(step.get("exercise") or ""))
    reps = max(1, int(step.get("reps") or 1))
    weight = step.get("weight_kg")
    built = create_strength_exercise_step(
        category,
        order,
        reps,
        exercise_name=exercise_name,
        weight_kg=float(weight) if weight else None,
    )
    if child_step_id is not None:
        built.childStepId = child_step_id
    # Keep the requested name visible even when the catalog lookup landed on a
    # generic category.
    note = step.get("note") or (step.get("exercise") if not exercise_name else None)
    if note:
        built.description = str(note)[:255]
    return built


def _build_structured_steps(
    steps: list[dict],
    profile: dict | None,
    max_hr_est: int | None,
    sport: Sport | None = None,
) -> list:
    """Expand the step schema into Garmin's step tree.

    Three things Garmin cares about that the library helpers don't handle:
    1. stepOrder is a flat, global counter over the whole segment — the repeat
       group itself consumes one, and its children continue the same sequence
       rather than restarting per group.
    2. Children of a repeat must carry childStepId matching their group's, or
       Connect flattens the group.
    3. A repeat's end condition is "iterations", with the count as its value.
    """
    order = 0
    group_id = 0

    def leaf(step: dict, child_step_id: int | None) -> ExecutableStep:
        nonlocal order
        order += 1
        kind = (step.get("kind") or "run").strip().lower()
        if kind == "exercise" or step.get("exercise"):
            return _build_exercise_step(step, order, child_step_id)
        type_id, type_key, display = _STEP_KINDS.get(kind, _STEP_KINDS["run"])
        end_condition, end_value = _end_condition(step)
        zone = _intensity_zone(step)
        extra = _target_for(zone, profile, max_hr_est, no_target=kind == "rest", sport=sport)
        if child_step_id is not None:
            extra["childStepId"] = child_step_id
        if step.get("note"):
            extra["description"] = str(step["note"])[:255]
        return ExecutableStep(
            stepOrder=order,
            stepType={"stepTypeId": type_id, "stepTypeKey": type_key, "displayOrder": display},
            endCondition=end_condition,
            endConditionValue=end_value,
            **extra,
        )

    def group(node: dict) -> RepeatGroup:
        nonlocal order, group_id
        order += 1
        group_id += 1
        # Snapshot both before building children — they advance the shared
        # counter, and the group keeps the order it claimed first.
        my_order, my_id = order, group_id
        iterations = max(1, int(node.get("repeat") or 1))
        children = [leaf(c, my_id) for c in (node.get("steps") or []) if isinstance(c, dict)]
        return RepeatGroup(
            stepOrder=my_order,
            stepType={"stepTypeId": StepType.REPEAT, "stepTypeKey": "repeat", "displayOrder": 6},
            numberOfIterations=iterations,
            workoutSteps=children,
            endCondition={
                "conditionTypeId": ConditionType.ITERATIONS,
                "conditionTypeKey": "iterations",
                "displayOrder": 7,
                "displayable": False,
            },
            endConditionValue=float(iterations),
            childStepId=my_id,
        )

    built: list = []
    for node in steps:
        if not isinstance(node, dict):
            continue
        if node.get("repeat") and node.get("steps"):
            built.append(group(node))
        else:
            built.append(leaf(node, None))
    return built


def _speed_scale(profile: dict | None) -> float:
    """Scale factor for the intensity->speed table, from the athlete's Garmin
    5K prediction. Purely for the duration estimate, never for targets."""
    races = (profile or {}).get("race_predictions_s") or {}
    t5k = races.get("5k")
    if not t5k:
        return 1.0
    speed_5k = 5000.0 / float(t5k)
    # A 5K is run around threshold-ish effort, so anchor the table's zone-4
    # speed to it and derive the scale from there.
    return max(0.5, min(2.0, speed_5k / _INTENSITY_SPEED[4]))


def _estimate_duration(steps: list[dict], profile: dict | None, sport: Sport | None = None) -> int:
    """Total expected duration, expanding repeats. Distance-based steps are
    converted with the per-intensity speed table; rep-based ones with a flat
    seconds-per-rep, which is all a strength estimate can honestly be."""
    scale = _speed_scale(profile)

    def leaf_seconds(step: dict) -> float:
        if step.get("duration_s"):
            return float(step["duration_s"])
        if step.get("reps"):
            return float(step["reps"]) * _SECONDS_PER_REP
        if step.get("distance_m"):
            speed = _INTENSITY_SPEED[_intensity_zone(step)] * scale
            return float(step["distance_m"]) / speed
        return 300.0  # lap-button step — assume a short block

    total = 0.0
    for node in steps:
        if not isinstance(node, dict):
            continue
        if node.get("repeat") and node.get("steps"):
            inner = sum(leaf_seconds(c) for c in node["steps"] if isinstance(c, dict))
            total += inner * max(1, int(node.get("repeat") or 1))
        else:
            total += leaf_seconds(node)
    return int(total)


def build_workout(
    workout_type: str,
    description: str | None,
    distance_m,
    pace_s_per_km,
    profile: dict | None = None,
    max_hr_est: int | None = None,
    steps: list[dict] | None = None,
    sport: str | Sport | None = None,
):
    """Build the payload for one workout in any supported sport.

    The sport decides three things and nothing else in here changes: which
    workout model wraps the payload, the sportType on the segment, and whether
    steps carry heart-rate targets (see app/garmin/sports.py)."""
    resolved = sport if isinstance(sport, Sport) else get_sport(sport)

    if steps:
        workout_steps = _build_structured_steps(steps, profile, max_hr_est, resolved)
        duration_s = _estimate_duration(steps, profile, resolved)
    else:
        distance = float(distance_m) if distance_m is not None else None
        pace = float(pace_s_per_km) if pace_s_per_km is not None else None
        # Pace no longer drives the step target (heart rate does), but it's
        # still the best estimate we have for the workout's expected duration.
        workout_steps = [_build_step(workout_type, distance, profile, max_hr_est, resolved)]

        if distance and pace:
            duration_s = int(distance / (1000.0 / pace))
        elif distance:
            duration_s = int(distance / 2.8)  # rough easy-pace fallback (~5:00/km)
        else:
            duration_s = 1800

    return resolved.workout_cls(
        workoutName=workout_type[:50],
        sportType=resolved.sport_type,
        estimatedDurationInSecs=duration_s,
        description=description[:255] if description else None,
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=resolved.sport_type,
                workoutSteps=workout_steps,
            )
        ],
    )



async def _remove_from_garmin(client, workout: PlanWorkout) -> None:
    """Unschedule and delete a workout's Garmin copy, clearing the local ids.

    Both calls are best-effort: the remote copy may already be gone (deleted in
    Connect, or on a device), in which case Garmin answers 404. That must not
    stop us — the point is to end up with no stale copy, and a workout that
    already doesn't exist satisfies that. Blocking here would make a
    reschedule fail permanently on rows whose Garmin copy was deleted
    behind our back."""
    if workout.garmin_scheduled_id:
        try:
            await asyncio.to_thread(client.unschedule_workout, workout.garmin_scheduled_id)
        except Exception:
            logger.warning(
                "Could not unschedule garmin workout for plan_workout id=%s (may already be gone)",
                workout.id,
            )
    try:
        await asyncio.to_thread(client.delete_workout, workout.garmin_workout_id)
    except Exception:
        logger.warning(
            "Could not delete garmin workout %s for plan_workout id=%s (may already be gone)",
            workout.garmin_workout_id,
            workout.id,
        )
    workout.garmin_workout_id = None
    workout.garmin_scheduled_id = None


async def _push_workout(client, workout: PlanWorkout, profile: dict | None, max_hr_est: int | None) -> None:
    """Build, upload and schedule one workout, stamping the Garmin ids onto the
    row. Raises on failure — callers decide how loud to be about it."""
    sport = get_sport(workout.sport)
    built = build_workout(
        workout.workout_type,
        workout.description,
        workout.target_distance_m,
        workout.target_pace_s_per_km,
        profile=profile,
        max_hr_est=max_hr_est,
        steps=workout.steps,
        sport=sport,
    )
    upload_result = await asyncio.to_thread(upload_callable(client, sport), built)
    workout_id = upload_result.get("workoutId")
    if workout_id is None:
        raise ValueError(f"no workoutId in upload response: {upload_result}")

    schedule_result = await asyncio.to_thread(
        client.schedule_workout, workout_id, workout.scheduled_date.isoformat()
    )
    scheduled_id = schedule_result.get("workoutScheduleId") or schedule_result.get("id")

    workout.garmin_workout_id = str(workout_id)
    workout.garmin_scheduled_id = str(scheduled_id) if scheduled_id is not None else None


async def sync_workout_to_garmin(db: AsyncSession, user: User, workout: PlanWorkout) -> dict:
    """Push a single workout to Garmin Connect immediately — the path used when
    the coach schedules a one-off session in chat, so the user never has to
    open the app and press Sync. Replaces any Garmin copy already attached to
    this row (e.g. rescheduling the same date)."""
    if not user.garmin_linked:
        return {"synced": False, "error": "garmin_not_linked"}
    if is_rest_day(workout.workout_type):
        return {"synced": False, "skipped": "rest_day"}

    try:
        client = await get_client(user.telegram_id)
        if workout.garmin_workout_id:
            await _remove_from_garmin(client, workout)
        profile = user.garmin_profile
        max_hr_est = await estimate_max_hr(db, user)
        await _push_workout(client, workout, profile, max_hr_est)
        await db.commit()
    except Exception as exc:
        logger.exception("Failed to push workout id=%s to Garmin", workout.id)
        await db.rollback()
        return {"synced": False, "error": str(exc)}

    return {"synced": True, "date": workout.scheduled_date.isoformat()}


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
            await _remove_from_garmin(client, w)
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

    # Resolve HR targets once for the whole push: the athlete's configured
    # zones (from their Garmin profile) if we have them, else a %-of-max band
    # off the highest HR we've observed in their activities.
    profile = user.garmin_profile
    max_hr_est = await estimate_max_hr(db, user)

    created = skipped_rest = skipped_already_synced = failed = 0

    for w in workouts:
        if w.garmin_workout_id:
            skipped_already_synced += 1
            continue
        if is_rest_day(w.workout_type):
            skipped_rest += 1
            continue

        try:
            await _push_workout(client, w, profile, max_hr_est)
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
