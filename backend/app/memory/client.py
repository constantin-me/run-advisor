import logging

from sqlalchemy import func, select

from app.db.models import Memory, User
from app.db.session import async_session

logger = logging.getLogger(__name__)


async def recall_memories(telegram_id: int, query: str, limit: int = 5) -> list[str]:
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            return []

        tsquery = func.plainto_tsquery("english", query)
        tsvector = func.to_tsvector("english", Memory.content)
        stmt = (
            select(Memory.content)
            .where(Memory.user_id == user.id, tsvector.op("@@")(tsquery))
            .order_by(func.ts_rank(tsvector, tsquery).desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


async def list_recent_memories(telegram_id: int, limit: int = 5) -> list[str]:
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            return []

        result = await db.execute(
            select(Memory.content).where(Memory.user_id == user.id).order_by(Memory.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def store_memory(telegram_id: int, fact: str) -> None:
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            logger.warning("store_memory called for unknown telegram_id=%s", telegram_id)
            return

        db.add(Memory(user_id=user.id, content=fact))
        await db.commit()
