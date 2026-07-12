from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db.models import PlanWorkout, TrainingPlan, User
from app.db.session import get_db
from app.garmin.workouts import sync_plan_to_garmin

router = APIRouter(prefix="/api/plan")


class WorkoutOut(BaseModel):
    id: int
    date: str
    type: str
    description: str | None
    distance_m: float | None
    pace_s_per_km: float | None
    done: bool
    synced_to_garmin: bool


class PlanOut(BaseModel):
    id: int
    title: str
    workouts: list[WorkoutOut]


class MarkDoneRequest(BaseModel):
    done: bool


@router.get("", response_model=PlanOut | None)
async def get_plan(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict | None:
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
            "id": w.id,
            "date": w.scheduled_date.isoformat(),
            "type": w.workout_type,
            "description": w.description,
            "distance_m": float(w.target_distance_m) if w.target_distance_m is not None else None,
            "pace_s_per_km": float(w.target_pace_s_per_km) if w.target_pace_s_per_km is not None else None,
            "done": w.done,
            "synced_to_garmin": w.garmin_workout_id is not None,
        }
        for w in wk_result.scalars().all()
    ]
    return {"id": plan.id, "title": plan.title, "workouts": workouts}


@router.post("/sync-garmin")
async def sync_garmin(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not user.garmin_linked:
        raise HTTPException(status_code=400, detail="Garmin not linked")
    return await sync_plan_to_garmin(db, user)


@router.patch("/workouts/{workout_id}")
async def mark_workout_done(
    workout_id: int,
    body: MarkDoneRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(PlanWorkout)
        .join(TrainingPlan, PlanWorkout.plan_id == TrainingPlan.id)
        .where(PlanWorkout.id == workout_id, TrainingPlan.user_id == user.id)
    )
    workout = result.scalar_one_or_none()
    if workout is None:
        raise HTTPException(status_code=404, detail="workout not found")

    workout.done = body.done
    await db.commit()
    return {"id": workout.id, "done": workout.done}
