"""The check-in only fires on a day that actually has data.

Before this, the daily push evaluated the previous day unconditionally, so a
morning with nothing synced still produced a confident note about stale
numbers. These tests run against a real (sqlite) database because the gate is
a query, not a pure function.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Activity, Base, DailyMetric, User
from app.scheduler import _local_day_bounds, has_live_data

DAY = date(2026, 9, 25)


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _user(db: AsyncSession, tz: str = "Europe/Bucharest") -> User:
    user = User(telegram_id=1, garmin_linked=True, timezone=tz)
    db.add(user)
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_no_data_at_all_means_no_push(db):
    user = await _user(db)
    assert await has_live_data(db, user, DAY, set()) is False


@pytest.mark.asyncio
async def test_empty_metric_row_does_not_count_as_data(db):
    user = await _user(db)
    # Garmin answers for any date asked about, so an all-null row exists even
    # on a day the watch never uploaded.
    db.add(DailyMetric(user_id=user.id, metric_date=DAY, raw={}))
    await db.commit()
    assert await has_live_data(db, user, DAY, set()) is False


@pytest.mark.asyncio
async def test_one_real_signal_is_enough(db):
    user = await _user(db)
    db.add(DailyMetric(user_id=user.id, metric_date=DAY, resting_hr=48, raw={}))
    await db.commit()
    assert await has_live_data(db, user, DAY, set()) is True


@pytest.mark.asyncio
async def test_yesterdays_data_does_not_make_today_live(db):
    user = await _user(db)
    db.add(DailyMetric(user_id=user.id, metric_date=DAY - timedelta(days=1), sleep_score=80, raw={}))
    await db.commit()
    assert await has_live_data(db, user, DAY, set()) is False


@pytest.mark.asyncio
async def test_suppressed_signal_cannot_be_the_only_evidence(db):
    user = await _user(db)
    db.add(DailyMetric(user_id=user.id, metric_date=DAY, sleep_score=80, raw={}))
    await db.commit()
    # The athlete told the coach to ignore sleep, so a sleep score alone is not
    # something it may write about.
    assert await has_live_data(db, user, DAY, {"sleep"}) is False
    assert await has_live_data(db, user, DAY, set()) is True


@pytest.mark.asyncio
async def test_an_activity_today_counts_even_without_metrics(db):
    user = await _user(db)
    db.add(
        Activity(
            user_id=user.id,
            garmin_activity_id="a1",
            activity_type="running",
            start_time=datetime(2026, 9, 25, 5, 30, tzinfo=timezone.utc),  # 08:30 local
            raw={},
        )
    )
    await db.commit()
    assert await has_live_data(db, user, DAY, set()) is True


@pytest.mark.asyncio
async def test_activity_outside_the_local_day_does_not_count(db):
    user = await _user(db)
    # 23:30 UTC on the 25th is already 02:30 on the 26th in Bucharest.
    db.add(
        Activity(
            user_id=user.id,
            garmin_activity_id="a2",
            activity_type="running",
            start_time=datetime(2026, 9, 25, 23, 30, tzinfo=timezone.utc),
            raw={},
        )
    )
    await db.commit()
    assert await has_live_data(db, user, DAY, set()) is False


def test_local_day_bounds_follow_the_users_timezone():
    user = User(telegram_id=1, timezone="Europe/Bucharest")
    lo, hi = _local_day_bounds(user, DAY)
    assert lo == datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc)
    assert hi == datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc)
