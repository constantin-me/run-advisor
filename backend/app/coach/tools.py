import logging
import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.coach.constraints import filter_metrics, filter_reasons, suppressed_topics_for
from app.coach.recovery import compute_recovery_status
from app.coach.verify import verify_plan, verify_workout
from app.db.models import Activity, DailyMetric, Goal, PlanWorkout, TrainingPlan, User
from app.garmin.sports import DEFAULT_SPORT, get_sport, sport_keys
from app.weather.client import geocode, get_forecast

logger = logging.getLogger(__name__)

# Sports the coach may schedule — the registry is the single source of truth,
# so a new sport there shows up in the tool schema automatically.
_SPORT_KEYS = sport_keys()

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_daily_metrics",
            "description": "Get the user's daily health metrics (sleep, HRV, resting HR, stress, body battery, VO2max, training readiness) for the last N days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days back, default 7",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_activities",
            "description": "Get the user's N most recent Garmin activities (runs etc.) with pace, distance, HR, and location (city/place name — null for indoor activities like treadmill or pool).",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Number of activities, default 5",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_goals",
            "description": (
                "Get the user's running goals. By default only active goals; "
                "pass include_completed=true to also see recently completed ones."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_completed": {
                        "type": "boolean",
                        "description": "If true, include completed goals as well as active ones",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_goal",
            "description": "Save a new running goal for the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Goal description, e.g. 'sub-50 10k'",
                    },
                    "target_date": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD, optional",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_goal",
            "description": (
                "Mark an active goal as completed after the user achieved it. "
                "Call this when activities clearly show the goal was met."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "integer",
                        "description": "Id of the goal to mark completed",
                    }
                },
                "required": ["goal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_checkin_time",
            "description": (
                "Set the local hour when the user receives their daily progress "
                "update / reminder push. Only whole hours (e.g. 7am, 12:00, 14:00, 2pm). "
                "Call this when they ask to change when they get updates or reminders."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "time": {
                        "type": "string",
                        "description": (
                            "Whole-hour time string, e.g. '7', '07:00', '7am', '2pm', '14:00'"
                        ),
                    }
                },
                "required": ["time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_training_plan",
            "description": "Get the user's current active training plan and its scheduled workouts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_training_plan",
            "description": "Create a new training plan with a list of scheduled workouts, replacing the currently active one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "goal_id": {
                        "type": "integer",
                        "description": "Optional goal id this plan targets",
                    },
                    "start_date": {
                        "type": "string",
                        "description": (
                            "ISO date the athlete asked the plan to start. Given, the saved plan "
                            "is checked against it and a partial plan is reported back to you."
                        ),
                    },
                    "end_date": {
                        "type": "string",
                        "description": (
                            "ISO date the athlete asked the plan to run through. Always set this "
                            "for a dated request ('8 weeks', 'until the race') — it is how a plan "
                            "that stops halfway gets caught."
                        ),
                    },
                    "workouts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "date": {
                                    "type": "string",
                                    "description": "ISO date YYYY-MM-DD",
                                },
                                "sport": {
                                    "type": "string",
                                    "enum": _SPORT_KEYS,
                                    "description": (
                                        "Sport for this session. Defaults to running. Use "
                                        "'strength' for gym sessions — never a run with fake steps."
                                    ),
                                },
                                "workout_type": {
                                    "type": "string",
                                    "description": (
                                        "e.g. easy run, recovery, long run, tempo, threshold, "
                                        "intervals, rest, upper body, leg day. This drives the "
                                        "heart-rate target pushed to the watch for HR-based "
                                        "sports, so use a clear, conventional name."
                                    ),
                                },
                                "description": {"type": "string"},
                                "distance_m": {"type": "number"},
                                "pace_s_per_km": {"type": "number"},
                                "steps": {
                                    "type": "array",
                                    "description": (
                                        "Optional structured breakdown, same schema as "
                                        "schedule_workout's steps. Required for any session with "
                                        "repeats, warm-up/cool-down, or intervals."
                                    ),
                                    "items": {"type": "object"},
                                },
                            },
                            "required": ["date", "workout_type"],
                        },
                    },
                },
                "required": ["title", "workouts"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_workout",
            "description": (
                "Schedule ONE workout on a specific date and push it straight to the user's "
                "Garmin watch. Use this for a single session ('give me intervals on Tuesday') — "
                "it adds to the existing plan without touching the rest of it. Use "
                "save_training_plan only when building a whole plan. For any structured session "
                "(N x something, warm-up/cool-down, intervals with recoveries) you MUST fill in "
                "`steps` so the breakdown reaches the watch instead of collapsing into one block."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                    "sport": {
                        "type": "string",
                        "enum": _SPORT_KEYS,
                        "description": (
                            "Sport for this session — decides what lands on the watch. Defaults "
                            "to running. A gym session is 'strength' with exercise steps."
                        ),
                    },
                    "workout_type": {
                        "type": "string",
                        "description": "Short name, e.g. 'sprint intervals', 'easy run', 'push day'",
                    },
                    "description": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "description": (
                            "Ordered step breakdown. A plain step has a `kind`, one end condition "
                            "(distance_m OR duration_s OR lap_button) and an `intensity`. A repeat "
                            "block instead has `repeat` (number of rounds) and its own nested "
                            "`steps`. Example — '3 x 200m sprint/200m jog, 3 x 400m sprint/400m "
                            "jog' with a warm-up and cool-down:\n"
                            '[{"kind":"warmup","duration_s":600,"intensity":"easy"},'
                            '{"repeat":3,"steps":[{"kind":"run","distance_m":200,"intensity":"sprint"},'
                            '{"kind":"recovery","distance_m":200,"intensity":"easy"}]},'
                            '{"repeat":3,"steps":[{"kind":"run","distance_m":400,"intensity":"sprint"},'
                            '{"kind":"recovery","distance_m":400,"intensity":"easy"}]},'
                            '{"kind":"cooldown","duration_s":600,"intensity":"easy"}]\n'
                            "For sport='strength', a step is an exercise with `exercise` (the "
                            "Garmin catalog display name), `reps` and optional `weight_kg`; a set "
                            "is a repeat block wrapping the exercise and its rest. Example — "
                            "4x10 bench press with 2min rest, then 3x12 rows:\n"
                            '[{"repeat":4,"steps":[{"kind":"exercise","exercise":"Barbell Bench Press",'
                            '"reps":10,"weight_kg":60},{"kind":"rest","duration_s":120}]},'
                            '{"repeat":3,"steps":[{"kind":"exercise","exercise":"Barbell Bent Over Row",'
                            '"reps":12},{"kind":"rest","duration_s":90}]}]\n'
                            "Use find_exercises when unsure an exercise name exists."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": [
                                        "warmup",
                                        "run",
                                        "recovery",
                                        "rest",
                                        "cooldown",
                                        "exercise",
                                    ],
                                },
                                "exercise": {
                                    "type": "string",
                                    "description": "Strength only — catalog display name, e.g. 'Barbell Deadlift'",
                                },
                                "reps": {
                                    "type": "integer",
                                    "description": "Strength only — repetitions in this set",
                                },
                                "weight_kg": {
                                    "type": "number",
                                    "description": "Strength only — target load, when the athlete gave one",
                                },
                                "distance_m": {"type": "number"},
                                "duration_s": {"type": "number"},
                                "lap_button": {
                                    "type": "boolean",
                                    "description": "End the step when the user presses lap",
                                },
                                "intensity": {
                                    "type": "string",
                                    "enum": [
                                        "recovery",
                                        "easy",
                                        "steady",
                                        "tempo",
                                        "threshold",
                                        "interval",
                                        "sprint",
                                        "max",
                                    ],
                                    "description": "Drives the custom heart-rate target for this step",
                                },
                                "note": {"type": "string"},
                                "repeat": {
                                    "type": "integer",
                                    "description": "Rounds — only on a repeat block, which also needs nested steps",
                                },
                                "steps": {
                                    "type": "array",
                                    "description": "Steps inside a repeat block",
                                    "items": {"type": "object"},
                                },
                            },
                        },
                    },
                    "distance_m": {
                        "type": "number",
                        "description": "Only for a simple unstructured run; ignored when steps is given",
                    },
                    "pace_s_per_km": {"type": "number"},
                },
                "required": ["date", "workout_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_location",
            "description": "Save the user's location (city) so weather-aware advice becomes available. Call this when the user mentions their city or when weather would be useful but no location is set yet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City name, e.g. 'Bucharest' or 'Austin, Texas'",
                    }
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather_forecast",
            "description": "Get the weather forecast (daily outlook + morning/midday/evening windows for the next 2 days) for the user's saved location. Returns an error if no location is set yet — ask the user for their city and call set_location first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days of daily outlook, default 3",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recovery_status",
            "description": "Check the user's current deterministic recovery flag (green/yellow/red) based on training readiness, HRV, and resting HR trends. Call this when the user mentions how they're feeling or asks whether they should train hard today. Returns null if there isn't enough recent data.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search long-term memory for facts previously learned about this user (injuries, preferences, past reactions to advice).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "store_memory",
            "description": (
                "Remember something durable about the user. kind='fact' for what you learned "
                "about them ('has a history of IT band issues'); kind='constraint' for an "
                "instruction they gave you about how to coach ('don't look at my sleep — I don't "
                "wear the watch at night'). Store a constraint the moment they ask you to stop "
                "doing something; it binds every future conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": (
                            "Written so it still makes sense months later, in their terms — for a "
                            "constraint, phrase it as the instruction itself plus the reason they "
                            "gave."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["fact", "constraint"],
                        "description": "Defaults to fact. Use constraint for 'stop/don't/never' instructions.",
                    },
                },
                "required": ["fact"],
            },
        },
    },
]


def _metric_to_dict(m: DailyMetric) -> dict[str, Any]:
    return {
        "date": m.metric_date.isoformat(),
        "sleep_score": m.sleep_score,
        "sleep_duration_s": (
            float(m.sleep_duration_s) if m.sleep_duration_s is not None else None
        ),
        "hrv": float(m.hrv) if m.hrv is not None else None,
        "resting_hr": m.resting_hr,
        "stress_avg": m.stress_avg,
        "body_battery": m.body_battery,
        "vo2max": float(m.vo2max) if m.vo2max is not None else None,
        "training_readiness": m.training_readiness,
    }


def _activity_to_dict(a: Activity) -> dict[str, Any]:
    return {
        "date": a.start_time.isoformat() if a.start_time else None,
        "type": a.activity_type,
        "distance_m": float(a.distance_m) if a.distance_m is not None else None,
        "duration_s": float(a.duration_s) if a.duration_s is not None else None,
        "avg_pace_s_per_km": (
            float(a.avg_pace_s_per_km) if a.avg_pace_s_per_km is not None else None
        ),
        "avg_hr": a.avg_hr,
        # None for indoor activities (treadmill, pool) — no GPS to report.
        "location": a.location_name,
    }


async def get_daily_metrics(db: AsyncSession, user: User, days: int = 7) -> list[dict]:
    since = date.today() - timedelta(days=days)
    result = await db.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user.id, DailyMetric.metric_date >= since)
        .order_by(DailyMetric.metric_date.desc())
    )
    metrics = [_metric_to_dict(m) for m in result.scalars().all()]
    # A "don't look at my sleep" instruction is enforced at the source: the
    # coach never receives the field, so it can't quote it by accident.
    return filter_metrics(metrics, await suppressed_topics_for(db, user))


async def get_recent_activities(db: AsyncSession, user: User, n: int = 5) -> list[dict]:
    result = await db.execute(
        select(Activity)
        .where(Activity.user_id == user.id)
        .order_by(Activity.start_time.desc())
        .limit(n)
    )
    return [_activity_to_dict(a) for a in result.scalars().all()]


async def get_goals(
    db: AsyncSession, user: User, *, include_completed: bool = False
) -> list[dict]:
    if include_completed:
        result = await db.execute(
            select(Goal)
            .where(Goal.user_id == user.id, Goal.status.in_(("active", "completed")))
            .order_by(Goal.created_at.desc())
        )
    else:
        result = await db.execute(
            select(Goal).where(Goal.user_id == user.id, Goal.status == "active")
        )
    return [
        {
            "id": g.id,
            "text": g.text,
            "target_date": g.target_date.isoformat() if g.target_date else None,
            "status": g.status,
        }
        for g in result.scalars().all()
    ]


async def save_goal(
    db: AsyncSession, user: User, text: str, target_date: str | None = None
) -> dict:
    parsed_date = date.fromisoformat(target_date) if target_date else None
    goal = Goal(user_id=user.id, text=text, target_date=parsed_date)
    db.add(goal)
    await db.commit()
    await db.refresh(goal)
    return {
        "id": goal.id,
        "text": goal.text,
        "target_date": target_date,
        "status": goal.status,
    }


async def complete_goal(db: AsyncSession, user: User, goal_id: int) -> dict:
    result = await db.execute(
        select(Goal).where(Goal.id == goal_id, Goal.user_id == user.id)
    )
    goal = result.scalar_one_or_none()
    if goal is None:
        return {"error": "not_found", "message": f"no goal with id {goal_id}"}
    if goal.status == "completed":
        return {
            "id": goal.id,
            "text": goal.text,
            "status": "completed",
            "already_completed": True,
        }
    goal.status = "completed"
    await db.commit()
    return {"id": goal.id, "text": goal.text, "status": "completed"}


_CHECKIN_TIME_RE = re.compile(
    r"^\s*(?:"
    r"(?P<h12>\d{1,2})\s*(?P<ampm>[ap]\.?m\.?)"
    r"|(?P<h24>\d{1,2})(?::(?P<min>\d{2}))?"
    r")\s*$",
    re.IGNORECASE,
)


def parse_checkin_hour(time_str: str) -> int | dict:
    """Parse a whole-hour time string into 0–23, or an error dict."""
    m = _CHECKIN_TIME_RE.match(time_str or "")
    if not m:
        return {
            "error": "invalid_time",
            "message": (
                "couldn't parse that time — use a whole hour like 7, 07:00, 7am, or 2pm"
            ),
        }

    if m.group("ampm"):
        hour = int(m.group("h12"))
        ampm = m.group("ampm").lower().replace(".", "")
        if hour < 1 or hour > 12:
            return {
                "error": "invalid_time",
                "message": "hour with am/pm must be 1–12",
            }
        if ampm == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        return hour

    hour = int(m.group("h24"))
    minute = m.group("min")
    if minute is not None and minute != "00":
        return {
            "error": "invalid_time",
            "message": "only whole hours are supported (e.g. 14:00, not 14:30)",
        }
    if hour < 0 or hour > 23:
        return {
            "error": "invalid_time",
            "message": "hour must be between 0 and 23",
        }
    return hour


def _format_hour_label(hour: int) -> str:
    if hour == 0:
        return "12:00 AM"
    if hour < 12:
        return f"{hour}:00 AM"
    if hour == 12:
        return "12:00 PM"
    return f"{hour - 12}:00 PM"


async def set_checkin_time(db: AsyncSession, user: User, time_str: str) -> dict:
    from app.config import settings

    parsed = parse_checkin_hour(time_str)
    if isinstance(parsed, dict):
        return parsed

    user.notification_hour = parsed
    await db.commit()
    tz = user.timezone or settings.tz
    return {
        "notification_hour": parsed,
        "local_time_label": _format_hour_label(parsed),
        "timezone": tz,
    }


async def get_training_plan(db: AsyncSession, user: User) -> dict | None:
    result = await db.execute(
        select(TrainingPlan)
        .where(TrainingPlan.user_id == user.id, TrainingPlan.status == "active")
        .order_by(TrainingPlan.created_at.desc())
    )
    plan = result.scalars().first()
    if plan is None:
        return None

    wk_result = await db.execute(
        select(PlanWorkout)
        .where(PlanWorkout.plan_id == plan.id)
        .order_by(PlanWorkout.scheduled_date)
    )
    workouts = [
        {
            "id": w.id,
            "date": w.scheduled_date.isoformat(),
            "sport": w.sport or DEFAULT_SPORT,
            "type": w.workout_type,
            "description": w.description,
            "steps": w.steps,
            "distance_m": (
                float(w.target_distance_m) if w.target_distance_m is not None else None
            ),
            "pace_s_per_km": (
                float(w.target_pace_s_per_km)
                if w.target_pace_s_per_km is not None
                else None
            ),
            "done": w.done,
            # Verification needs to know what actually reached the watch — the
            # coach must never report a session as synced on faith.
            "synced_to_garmin": w.garmin_workout_id is not None,
        }
        for w in wk_result.scalars().all()
    ]
    return {"id": plan.id, "title": plan.title, "workouts": workouts}


async def save_training_plan(
    db: AsyncSession,
    user: User,
    title: str,
    workouts: list[dict],
    goal_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    old_plans = await db.execute(
        select(TrainingPlan).where(
            TrainingPlan.user_id == user.id, TrainingPlan.status == "active"
        )
    )
    old_plan_ids = [old.id for old in old_plans.scalars().all()]

    # A regenerated plan is a fresh set of rows, so ticked-off sessions would
    # silently come back undone. Carry the completions over by date — the run
    # you actually did on Monday stays done even if the coach reworded or
    # retargeted that day's session.
    completed_dates: set[date] = set()
    if old_plan_ids:
        done_rows = await db.execute(
            select(PlanWorkout.scheduled_date).where(
                PlanWorkout.plan_id.in_(old_plan_ids), PlanWorkout.done.is_(True)
            )
        )
        completed_dates = set(done_rows.scalars().all())

        await db.execute(
            update(TrainingPlan)
            .where(TrainingPlan.id.in_(old_plan_ids))
            .values(status="superseded")
        )

    plan = TrainingPlan(user_id=user.id, goal_id=goal_id, title=title, status="active")
    db.add(plan)
    await db.flush()

    for w in workouts:
        scheduled = date.fromisoformat(w["date"])
        db.add(
            PlanWorkout(
                plan_id=plan.id,
                scheduled_date=scheduled,
                workout_type=w["workout_type"],
                sport=get_sport(w.get("sport")).key,
                description=w.get("description"),
                target_distance_m=w.get("distance_m"),
                target_pace_s_per_km=w.get("pace_s_per_km"),
                steps=w.get("steps"),
                done=scheduled in completed_dates,
            )
        )

    await db.commit()
    # Push straight to the watch — the user shouldn't have to open the app and
    # press Sync. Failure-safe: a Garmin problem is reported in the result, not
    # raised, so the coach can still answer.
    garmin = await _autosync_plan(db, user)
    # Audit what actually landed in the database rather than what was intended:
    # the result the coach reads is the saved state, not its own request.
    verification = verify_plan(
        await get_training_plan(db, user),
        requested_start=start_date,
        requested_end=end_date,
    )
    return {
        "id": plan.id,
        "title": title,
        "workout_count": len(workouts),
        "garmin": garmin,
        "verification": verification,
    }


async def _autosync_plan(db: AsyncSession, user: User) -> dict:
    from app.garmin.workouts import sync_plan_to_garmin

    if not user.garmin_linked:
        return {"synced": False, "error": "garmin_not_linked"}
    try:
        return {"synced": True, **await sync_plan_to_garmin(db, user)}
    except Exception as exc:
        logger.exception("Auto-sync to Garmin failed for user_id=%s", user.id)
        return {"synced": False, "error": str(exc)}


async def schedule_workout(
    db: AsyncSession,
    user: User,
    date_str: str,
    workout_type: str,
    sport: str | None = None,
    description: str | None = None,
    steps: list | None = None,
    distance_m: float | None = None,
    pace_s_per_km: float | None = None,
) -> dict:
    """Add or replace a single dated workout inside the user's active plan, then
    push just that workout to Garmin. Unlike save_training_plan this never
    supersedes the existing plan — asking for one Tuesday session must not wipe
    a whole training block."""
    from app.garmin.workouts import sync_workout_to_garmin

    scheduled = date.fromisoformat(date_str)

    result = await db.execute(
        select(TrainingPlan)
        .where(TrainingPlan.user_id == user.id, TrainingPlan.status == "active")
        .order_by(TrainingPlan.created_at.desc())
    )
    plan = result.scalars().first()
    if plan is None:
        plan = TrainingPlan(user_id=user.id, title="Ad-hoc workouts", status="active")
        db.add(plan)
        await db.flush()

    existing = await db.execute(
        select(PlanWorkout).where(
            PlanWorkout.plan_id == plan.id, PlanWorkout.scheduled_date == scheduled
        )
    )
    workout = existing.scalars().first()
    replaced = workout is not None
    if workout is None:
        workout = PlanWorkout(plan_id=plan.id, scheduled_date=scheduled)
        db.add(workout)

    # Only clear a completion when the session genuinely changed — re-pushing or
    # rewording the same day's workout must not un-tick a run they already did.
    if workout.done and workout.workout_type.strip().lower() != workout_type.strip().lower():
        workout.done = False

    workout.workout_type = workout_type
    workout.sport = get_sport(sport).key
    workout.description = description
    workout.steps = steps
    workout.target_distance_m = distance_m
    workout.target_pace_s_per_km = pace_s_per_km
    await db.commit()

    # Read anything we need for the response *before* the Garmin push: on
    # failure it rolls back, which expires these ORM objects, and touching an
    # expired attribute afterwards triggers a lazy refresh (illegal here — it
    # blows up with MissingGreenlet in async context).
    plan_title = plan.title
    saved = {
        "date": workout.scheduled_date.isoformat(),
        "sport": workout.sport,
        "type": workout.workout_type,
        "steps": workout.steps,
        "distance_m": float(workout.target_distance_m) if workout.target_distance_m is not None else None,
        "pace_s_per_km": (
            float(workout.target_pace_s_per_km) if workout.target_pace_s_per_km is not None else None
        ),
    }
    garmin = await sync_workout_to_garmin(db, user, workout)
    return {
        "date": scheduled.isoformat(),
        "workout_type": workout_type,
        "plan": plan_title,
        "replaced_existing": replaced,
        "step_count": len(steps) if steps else 0,
        "garmin": garmin,
        "verification": verify_workout(saved, garmin),
    }


async def set_location(db: AsyncSession, user: User, city: str) -> dict:
    resolved = await geocode(city)
    if resolved is None:
        return {
            "error": "not_found",
            "message": f"couldn't find a location matching {city!r}",
        }

    user.latitude = resolved["latitude"]
    user.longitude = resolved["longitude"]
    user.location_name = (
        f"{resolved['name']}, {resolved['country']}"
        if resolved.get("country")
        else resolved["name"]
    )
    user.timezone = resolved.get("timezone")
    await db.commit()

    return {"location_name": user.location_name}


async def get_weather_forecast(db: AsyncSession, user: User, days: int = 3) -> dict:
    if user.latitude is None or user.longitude is None:
        return {
            "error": "no_location",
            "message": "ask the user for their city, then call set_location",
        }

    forecast = await get_forecast(
        float(user.latitude), float(user.longitude), user.timezone or "UTC", days=days
    )
    if forecast is None:
        return {"error": "weather_unavailable"}

    return {"location": user.location_name, **forecast}


async def get_recovery_status(db: AsyncSession, user: User) -> dict:
    status = await compute_recovery_status(db, user)
    if status is None:
        return {
            "status": "unknown",
            "message": "not enough recent data — try /sync first",
        }

    suppressed = await suppressed_topics_for(db, user)
    return {
        "level": status.level,
        "reasons": filter_reasons(status.reasons, suppressed),
        "metric_date": status.metric_date.isoformat(),
        "training_readiness": None if "training_readiness" in suppressed else status.training_readiness,
        "hrv_delta_pct": None if "hrv" in suppressed else status.hrv_delta_pct,
        "rhr_delta": None if "resting_hr" in suppressed else status.rhr_delta,
    }


TOOL_SCHEMAS.append(
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": (
                "Remove a stored fact or standing instruction by its id, when the user lifts or "
                "replaces it ('you can look at my sleep again'). Standing instructions are listed "
                "with their ids in your context."
            ),
            "parameters": {
                "type": "object",
                "properties": {"memory_id": {"type": "integer"}},
                "required": ["memory_id"],
            },
        },
    }
)

TOOL_SCHEMAS.append(
    {
        "type": "function",
        "function": {
            "name": "verify_training_plan",
            "description": (
                "Audit the athlete's active plan as it is actually stored: coverage of a "
                "requested date range, workout count, sports, structured steps, rest spacing, "
                "weekly volume and progression, and what really reached the watch. Call this "
                "before telling the athlete a plan is done, and after any correction."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "ISO date the plan was asked to start (optional)",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "ISO date the plan was asked to run through (optional)",
                    },
                },
            },
        },
    }
)

TOOL_SCHEMAS.append(
    {
        "type": "function",
        "function": {
            "name": "find_exercises",
            "description": (
                "Search Garmin's strength-exercise catalog by name. Use it before building a "
                "strength workout when unsure whether an exercise exists under that name — the "
                "watch shows a generic category for anything it can't match."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "Part of an exercise name, e.g. 'bench press', 'squat'",
                    }
                },
                "required": ["term"],
            },
        },
    }
)


def find_exercises(term: str, limit: int = 25) -> dict:
    """Catalog names matching `term`, so the coach schedules exercises Garmin
    actually knows instead of inventing labels."""
    from garminconnect import exercises as exercise_catalog

    matches = exercise_catalog.find(term)
    names = [e["name"] for e in matches[:limit]]
    return {"term": term, "total": len(matches), "names": names}


async def execute_tool(db: AsyncSession, user: User, name: str, arguments: dict) -> Any:
    from app.memory.client import recall_memories, store_memory as memory_store

    if name == "get_daily_metrics":
        return await get_daily_metrics(db, user, days=arguments.get("days", 7))
    if name == "get_recent_activities":
        return await get_recent_activities(db, user, n=arguments.get("n", 5))
    if name == "get_goals":
        return await get_goals(
            db, user, include_completed=bool(arguments.get("include_completed", False))
        )
    if name == "save_goal":
        return await save_goal(
            db, user, text=arguments["text"], target_date=arguments.get("target_date")
        )
    if name == "complete_goal":
        return await complete_goal(db, user, goal_id=arguments["goal_id"])
    if name == "set_checkin_time":
        return await set_checkin_time(db, user, time_str=arguments["time"])
    if name == "get_training_plan":
        return await get_training_plan(db, user)
    if name == "save_training_plan":
        return await save_training_plan(
            db,
            user,
            title=arguments["title"],
            workouts=arguments["workouts"],
            goal_id=arguments.get("goal_id"),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
        )
    if name == "schedule_workout":
        return await schedule_workout(
            db,
            user,
            date_str=arguments["date"],
            workout_type=arguments["workout_type"],
            sport=arguments.get("sport"),
            description=arguments.get("description"),
            steps=arguments.get("steps"),
            distance_m=arguments.get("distance_m"),
            pace_s_per_km=arguments.get("pace_s_per_km"),
        )
    if name == "find_exercises":
        return find_exercises(arguments["term"])
    if name == "verify_training_plan":
        return verify_plan(
            await get_training_plan(db, user),
            requested_start=arguments.get("start_date"),
            requested_end=arguments.get("end_date"),
        )
    if name == "set_location":
        return await set_location(db, user, city=arguments["city"])
    if name == "get_weather_forecast":
        return await get_weather_forecast(db, user, days=arguments.get("days", 3))
    if name == "get_recovery_status":
        return await get_recovery_status(db, user)
    if name == "search_memory":
        return await recall_memories(user.telegram_id, arguments["query"])
    if name == "store_memory":
        kind = arguments.get("kind", "fact")
        memory_id = await memory_store(user.telegram_id, arguments["fact"], kind=kind)
        return {"stored": True, "id": memory_id, "kind": kind}
    if name == "forget_memory":
        from app.memory.client import forget_memory

        removed = await forget_memory(user.telegram_id, int(arguments["memory_id"]))
        return {"removed": removed}

    return {"error": f"unknown tool {name}"}
