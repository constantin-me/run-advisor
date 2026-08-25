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
    """Recently learned facts. Standing instructions are deliberately excluded —
    they are injected in full by app/coach/constraints.py, not sampled."""
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            return []

        result = await db.execute(
            select(Memory.content)
            .where(Memory.user_id == user.id, Memory.kind == "fact")
            .order_by(Memory.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def store_memory(telegram_id: int, fact: str, kind: str = "fact") -> int | None:
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            logger.warning("store_memory called for unknown telegram_id=%s", telegram_id)
            return None

        memory = Memory(user_id=user.id, content=fact, kind=kind if kind == "constraint" else "fact")
        db.add(memory)
        await db.commit()
        return memory.id


async def forget_memory(telegram_id: int, memory_id: int) -> bool:
    """Drop a stored fact or standing instruction — the athlete can lift a rule
    they set ("actually, do use my sleep again")."""
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            return False

        memory = (
            await db.execute(
                select(Memory).where(Memory.id == memory_id, Memory.user_id == user.id)
            )
        ).scalar_one_or_none()
        if memory is None:
            return False

        await db.delete(memory)
        await db.commit()
        return True
