import asyncio
import logging
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.config import settings
from app.db.models import User
from app.db.session import async_session
from app.garmin.sync import sync_day
from app.weather.client import get_forecast

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=settings.tz)


async def run_user_progress(user_id: int, day: date) -> None:
    from app.coach.agent import generate_progress_summary
    from app.telegram.bot import push_message

    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()

        await sync_day(db, user, day)

        instruction = (
            f"This is the automated daily check-in for {day.isoformat()}. Compare today's synced "
            "metrics against the user's active goals and training plan, and write a short "
            "(2-4 sentence) progress update for them, phrased for a push notification, not a "
            "conversation. If something in the data is worth remembering long-term, call store_memory."
        )

        # get_forecast never raises (it swallows and logs failures), so this
        # is safe to call unconditionally when a location is set. Uses the
        # user's own timezone (from geocoding) so "upcoming" is their local
        # next day, not UTC's — the job itself fires at UTC midnight.
        if user.latitude is not None and user.longitude is not None:
            forecast = await get_forecast(
                float(user.latitude), float(user.longitude), user.timezone or "UTC", days=2
            )
            if forecast and forecast.get("daily"):
                upcoming = forecast["daily"][0]
                instruction += (
                    f"\n\nUpcoming weather ({upcoming['date']}, local time): "
                    f"{upcoming['temp_min']:.0f}-{upcoming['temp_max']:.0f}°C "
                    f"(feels like up to {upcoming['feels_like_max']:.0f}°C), "
                    f"{upcoming['precipitation_probability']}% rain, {upcoming['condition']}. "
                    "If conditions are extreme (heat, storms, cold), factor that into the update — "
                    "e.g. suggest an earlier start time or an easier effort."
                )

        summary = await generate_progress_summary(db, user, instruction)

    if summary:
        try:
            await push_message(user.telegram_id, summary)
        except Exception:
            logger.exception("Failed to push progress message to telegram_id=%s", user.telegram_id)


async def run_daily_progress_job() -> None:
    yesterday = date.today() - timedelta(days=1)
    async with async_session() as db:
        result = await db.execute(select(User.id).where(User.garmin_linked.is_(True)))
        user_ids = list(result.scalars().all())

    for user_id in user_ids:
        try:
            await run_user_progress(user_id, yesterday)
        except Exception:
            logger.exception("Daily progress job failed for user_id=%s", user_id)


def register_jobs() -> None:
    scheduler.add_job(
        run_daily_progress_job,
        CronTrigger(hour=0, minute=0),
        id="midnight_progress",
        replace_existing=True,
    )


if __name__ == "__main__":
    import sys

    if "--run-now" in sys.argv:
        asyncio.run(run_daily_progress_job())
    else:
        print("Usage: python -m app.scheduler --run-now")
