from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Activity, DailyMetric, Goal, PlanWorkout, TrainingPlan, User

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_daily_metrics",
            "description": "Get the user's daily health metrics (sleep, HRV, resting HR, stress, body battery, VO2max, training readiness) for the last N days.",
            "parameters": {
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "Number of days back, default 7"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_activities",
            "description": "Get the user's N most recent Garmin activities (runs etc.) with pace, distance, HR.",
            "parameters": {
                "type": "object",
                "properties": {"n": {"type": "integer", "description": "Number of activities, default 5"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_goals",
            "description": "Get the user's active running goals.",
            "parameters": {"type": "object", "properties": {}},
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
                    "text": {"type": "string", "description": "Goal description, e.g. 'sub-50 10k'"},
                    "target_date": {"type": "string", "description": "ISO date YYYY-MM-DD, optional"},
                },
                "required": ["text"],
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
                    "goal_id": {"type": "integer", "description": "Optional goal id this plan targets"},
                    "workouts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                                "workout_type": {"type": "string", "description": "e.g. easy run, tempo, long run, rest"},
                                "description": {"type": "string"},
                                "distance_m": {"type": "number"},
                                "pace_s_per_km": {"type": "number"},
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
            "description": "Store a durable fact about the user for future conversations (e.g. 'has a history of IT band issues').",
            "parameters": {
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
        },
    },
]


def _metric_to_dict(m: DailyMetric) -> dict[str, Any]:
    return {
        "date": m.metric_date.isoformat(),
        "sleep_score": m.sleep_score,
        "sleep_duration_s": float(m.sleep_duration_s) if m.sleep_duration_s is not None else None,
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
        "avg_pace_s_per_km": float(a.avg_pace_s_per_km) if a.avg_pace_s_per_km is not None else None,
        "avg_hr": a.avg_hr,
    }


async def get_daily_metrics(db: AsyncSession, user: User, days: int = 7) -> list[dict]:
    since = date.today() - timedelta(days=days)
    result = await db.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user.id, DailyMetric.metric_date >= since)
        .order_by(DailyMetric.metric_date.desc())
    )
    return [_metric_to_dict(m) for m in result.scalars().all()]


async def get_recent_activities(db: AsyncSession, user: User, n: int = 5) -> list[dict]:
    result = await db.execute(
        select(Activity).where(Activity.user_id == user.id).order_by(Activity.start_time.desc()).limit(n)
    )
    return [_activity_to_dict(a) for a in result.scalars().all()]


async def get_goals(db: AsyncSession, user: User) -> list[dict]:
    result = await db.execute(select(Goal).where(Goal.user_id == user.id, Goal.status == "active"))
    return [
        {"id": g.id, "text": g.text, "target_date": g.target_date.isoformat() if g.target_date else None}
        for g in result.scalars().all()
    ]


async def save_goal(db: AsyncSession, user: User, text: str, target_date: str | None = None) -> dict:
    parsed_date = date.fromisoformat(target_date) if target_date else None
    goal = Goal(user_id=user.id, text=text, target_date=parsed_date)
    db.add(goal)
    await db.commit()
    await db.refresh(goal)
    return {"id": goal.id, "text": goal.text, "target_date": target_date}


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
        select(PlanWorkout).where(PlanWorkout.plan_id == plan.id).order_by(PlanWorkout.scheduled_date)
    )
    workouts = [
        {
            "date": w.scheduled_date.isoformat(),
            "type": w.workout_type,
            "description": w.description,
            "distance_m": float(w.target_distance_m) if w.target_distance_m is not None else None,
            "pace_s_per_km": float(w.target_pace_s_per_km) if w.target_pace_s_per_km is not None else None,
            "done": w.done,
        }
        for w in wk_result.scalars().all()
    ]
    return {"id": plan.id, "title": plan.title, "workouts": workouts}


async def save_training_plan(
    db: AsyncSession, user: User, title: str, workouts: list[dict], goal_id: int | None = None
) -> dict:
    old_plans = await db.execute(
        select(TrainingPlan).where(TrainingPlan.user_id == user.id, TrainingPlan.status == "active")
    )
    for old in old_plans.scalars().all():
        old.status = "superseded"

    plan = TrainingPlan(user_id=user.id, goal_id=goal_id, title=title, status="active")
    db.add(plan)
    await db.flush()

    for w in workouts:
        db.add(
            PlanWorkout(
                plan_id=plan.id,
                scheduled_date=date.fromisoformat(w["date"]),
                workout_type=w["workout_type"],
                description=w.get("description"),
                target_distance_m=w.get("distance_m"),
                target_pace_s_per_km=w.get("pace_s_per_km"),
            )
        )

    await db.commit()
    return {"id": plan.id, "title": title, "workout_count": len(workouts)}


async def execute_tool(db: AsyncSession, user: User, name: str, arguments: dict) -> Any:
    from app.memory.client import recall_memories, store_memory as memory_store

    if name == "get_daily_metrics":
        return await get_daily_metrics(db, user, days=arguments.get("days", 7))
    if name == "get_recent_activities":
        return await get_recent_activities(db, user, n=arguments.get("n", 5))
    if name == "get_goals":
        return await get_goals(db, user)
    if name == "save_goal":
        return await save_goal(db, user, text=arguments["text"], target_date=arguments.get("target_date"))
    if name == "get_training_plan":
        return await get_training_plan(db, user)
    if name == "save_training_plan":
        return await save_training_plan(
            db, user, title=arguments["title"], workouts=arguments["workouts"], goal_id=arguments.get("goal_id")
        )
    if name == "search_memory":
        return await recall_memories(user.telegram_id, arguments["query"])
    if name == "store_memory":
        await memory_store(user.telegram_id, arguments["fact"])
        return {"stored": True}

    return {"error": f"unknown tool {name}"}
