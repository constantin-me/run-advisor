"""Dormancy, weekly nudges, and the mute the athlete controls.

A daily check-in aimed at someone who isn't training reads as nagging, and
once they've said "not now", only training should bring the messages back.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Activity, Base, TrainingPlan, User
from app.scheduler import DORMANT_AFTER, clear_mute_if_returned, is_dormant

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _user(db: AsyncSession, **kwargs) -> User:
    user = User(telegram_id=1, garmin_linked=True, timezone="UTC", **kwargs)
    db.add(user)
    await db.commit()
    return user


async def _activity(db: AsyncSession, user: User, when: datetime) -> None:
    db.add(
        Activity(
            user_id=user.id,
            garmin_activity_id=f"a{when.timestamp()}",
            activity_type="running",
            start_time=when,
            raw={},
        )
    )
    await db.commit()


async def _plan(db: AsyncSession, user: User, status: str = "active") -> None:
    db.add(TrainingPlan(user_id=user.id, title="Plan", status=status))
    await db.commit()


@pytest.mark.asyncio
async def test_no_plan_means_dormant(db):
    user = await _user(db)
    await _activity(db, user, NOW - timedelta(days=1))
    assert await is_dormant(db, user) is True


@pytest.mark.asyncio
async def test_plan_but_nothing_logged_recently_means_dormant(db):
    user = await _user(db)
    await _plan(db, user)
    await _activity(db, user, NOW - DORMANT_AFTER - timedelta(days=1))
    assert await is_dormant(db, user) is True


@pytest.mark.asyncio
async def test_superseded_plan_does_not_count(db):
    user = await _user(db)
    await _plan(db, user, status="superseded")
    await _activity(db, user, NOW - timedelta(hours=2))
    assert await is_dormant(db, user) is True


@pytest.mark.asyncio
async def test_active_plan_and_recent_training_is_not_dormant(db):
    user = await _user(db)
    await _plan(db, user)
    await _activity(db, user, NOW - timedelta(days=2))
    assert await is_dormant(db, user) is False


@pytest.mark.asyncio
async def test_mute_survives_until_they_train_again(db):
    muted_at = NOW - timedelta(days=3)
    user = await _user(db, nudges_muted_at=muted_at, nudges_muted_reason="flu")
    # An activity from before the mute is not them coming back.
    await _activity(db, user, muted_at - timedelta(days=1))
    assert await clear_mute_if_returned(db, user) is False
    assert user.nudges_muted_at is not None


@pytest.mark.asyncio
async def test_a_workout_after_the_mute_lifts_it(db):
    muted_at = NOW - timedelta(days=3)
    user = await _user(db, nudges_muted_at=muted_at, nudges_muted_reason="not in the mood")
    await _activity(db, user, muted_at + timedelta(days=2))
    assert await clear_mute_if_returned(db, user) is True
    assert user.nudges_muted_at is None
    assert user.nudges_muted_reason is None


@pytest.mark.asyncio
async def test_clearing_is_a_no_op_when_not_muted(db):
    user = await _user(db)
    await _activity(db, user, NOW)
    assert await clear_mute_if_returned(db, user) is False
