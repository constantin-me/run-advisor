from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.coach.agent import stream_chat
from app.db.models import ChatMessage, User
from app.db.session import get_db

router = APIRouter(prefix="/api/chat")


class ChatRequest(BaseModel):
    text: str


class ChatMessageOut(BaseModel):
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/history", response_model=list[ChatMessageOut])
async def get_history(
    limit: int = 200,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChatMessage]:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())[::-1]


@router.post("/stream")
async def chat_stream(
    body: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    return StreamingResponse(stream_chat(db, user, body.text), media_type="text/plain")


@router.delete("/history")
async def clear_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Wipes this user's chat_messages. Useful after a system-prompt/behavior
    change, since the model otherwise anchors on its own earlier answers
    still sitting in history."""
    await db.execute(delete(ChatMessage).where(ChatMessage.user_id == user.id))
    await db.commit()
    return {"status": "ok"}
