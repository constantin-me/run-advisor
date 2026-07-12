from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.coach.tools import get_goals, save_goal
from app.db.models import User
from app.db.session import get_db

router = APIRouter(prefix="/api/goals")


class GoalOut(BaseModel):
    id: int
    text: str
    target_date: str | None


class CreateGoalRequest(BaseModel):
    text: str
    target_date: str | None = None


@router.get("", response_model=list[GoalOut])
async def list_goals(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    return await get_goals(db, user)


@router.post("", response_model=GoalOut)
async def create_goal(
    body: CreateGoalRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await save_goal(db, user, text=body.text, target_date=body.target_date)
