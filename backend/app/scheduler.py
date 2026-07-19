import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.coach.recovery import compute_recovery_status
from app.config import settings
from app.db.models import User
from app.db.session import async_session
from app.garmin.profile import refresh_profile
from app.garmin.sync import sync_day
from app.weather.client import get_forecast

logger = logging.getLogger(__name__)

# Profile data (zones, VO2max, race predictions, weight) changes slowly; a
# weekly refresh in the nightly job is plenty.
PROFILE_REFRESH_AFTER = timedelta(days=7)

scheduler = AsyncIOScheduler(timezone=settings.tz)


async def run_user_progress(user_id: int, day: date) -> None:
    from app.coach.agent import generate_progress_summary
    from app.telegram.bot import push_message

    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()

        await sync_day(db, user, day)

        last_profile = user.profile_synced_at
        if last_profile is not None and last_profile.tzinfo is None:
            last_profile = last_profile.replace(tzinfo=timezone.utc)
        if last_profile is None or datetime.now(timezone.utc) - last_profile >= PROFILE_REFRESH_AFTER:
            await refresh_profile(db, user)

        instruction = (
            f"This is the automated daily check-in for {day.isoformat()}. Compare today's synced "
            "metrics against the user's active goals and training plan, and write a short "
            "(2-4 sentence) progress update for them, phrased for a push notification, not a "
            "conversation. If something in the data is worth remembering long-term, call store_memory."
        )

        # Deterministic trigger for the push to actually lead with a recovery
        # flag, rather than hoping the LLM notices it in the data dump.
        try:
            recovery = await compute_recovery_status(db, user)
        except Exception:
            logger.exception(
                "Recovery status computation failed for user_id=%s", user.id
            )
            recovery = None

        if recovery is not None and recovery.level != "green":
            reasons = (
                "; ".join(recovery.reasons)
                if recovery.reasons
                else "no specific signal recorded"
            )
            instruction = (
                f"IMPORTANT: recovery signals for {recovery.metric_date.isoformat()} are flagged "
                f"({recovery.level}) — reasons: {reasons}. Lead the message with this and give one "
                "concrete recommendation (easy day, rest, extra sleep) before anything else.\n\n"
            ) + instruction

        # get_forecast never raises (it swallows and logs failures), so this
        # is safe to call unconditionally when a location is set. Uses the
        # user's own timezone (from geocoding) so "upcoming" is their local
        # next day, not UTC's — the job itself fires at UTC midnight.
        if user.latitude is not None and user.longitude is not None:
            forecast = await get_forecast(
                float(user.latitude),
                float(user.longitude),
                user.timezone or "UTC",
                days=2,
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
            logger.exception(
                "Failed to push progress message to telegram_id=%s", user.telegram_id
            )


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


# --- Bi-weekly progress evaluation ---------------------------------------

PROGRESS_EVAL_EVERY = timedelta(days=14)


def _fmt_delta(value, unit: str, *, lower_is_better: bool) -> str:
    if value is None:
        return "no prior data"
    if value == 0:
        return "flat"
    better = (value < 0) if lower_is_better else (value > 0)
    arrow = "↓" if value < 0 else "↑"
    tag = "better" if better else "worse"
    return f"{arrow}{abs(value)}{unit} ({tag})"


def _progress_instruction(p: dict) -> str:
    c, d = p["current"], p["deltas"]
    lines = [
        f"This is the {p['period_days']}-day progress check-in. Deterministic stats, "
        f"most recent {p['period_days']} days vs the {p['period_days']} before:",
        f"- Volume: {c['distance_km']} km over {c['runs']} runs "
        f"({_fmt_delta(d['distance_km'], ' km', lower_is_better=False)})",
        f"- Avg pace: {c['avg_pace_s_per_km']}s/km ({_fmt_delta(d['avg_pace_s_per_km'], 's/km', lower_is_better=True)})",
        f"- Avg run HR: {c['avg_hr']} ({_fmt_delta(d['avg_hr'], ' bpm', lower_is_better=True)})",
        f"- Resting HR: {c['avg_resting_hr']} ({_fmt_delta(d['avg_resting_hr'], ' bpm', lower_is_better=True)})",
        f"- VO2max: {c['vo2max']} ({_fmt_delta(d['vo2max'], '', lower_is_better=False)})",
    ]
    return (
        "\n".join(lines)
        + "\n\nWrite a short (2-4 sentence) progress note for a push notification: what improved, "
        "what slipped, and one concrete focus for the next two weeks. Lead with the headline. "
        "No fluff."
    )


async def run_user_progress_eval(user_id: int) -> None:
    from app.coach.agent import generate_progress_summary
    from app.coach.progress import compute_progress
    from app.telegram.bot import push_message

    async with async_session() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        progress = await compute_progress(db, user)
        if progress is None:
            return  # not enough running yet — try again next cycle

        summary = await generate_progress_summary(db, user, _progress_instruction(progress))

        user.last_progress_eval_at = datetime.now(timezone.utc)
        await db.commit()

    if summary:
        try:
            await push_message(user.telegram_id, summary)
        except Exception:
            logger.exception("Failed to push progress-eval to telegram_id=%s", user.telegram_id)


async def run_progress_eval_job() -> None:
    """Runs weekly, but each user is only evaluated once per PROGRESS_EVAL_EVERY
    (14 days) — gated on their own last_progress_eval_at, so cadence is
    per-user and survives restarts."""
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        rows = (
            await db.execute(
                select(User.id, User.last_progress_eval_at).where(User.garmin_linked.is_(True))
            )
        ).all()

    for user_id, last in rows:
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < PROGRESS_EVAL_EVERY:
                continue
        try:
            await run_user_progress_eval(user_id)
        except Exception:
            logger.exception("Progress-eval job failed for user_id=%s", user_id)


def register_jobs() -> None:
    scheduler.add_job(
        run_daily_progress_job,
        CronTrigger(hour=0, minute=0),
        id="midnight_progress",
        replace_existing=True,
    )
    # Weekly sweep; per-user 14-day gating lives inside the job.
    scheduler.add_job(
        run_progress_eval_job,
        CronTrigger(day_of_week="mon", hour=7, minute=0),
        id="progress_evaluation",
        replace_existing=True,
    )


if __name__ == "__main__":
    import sys

    if "--run-now" in sys.argv:
        asyncio.run(run_daily_progress_job())
    elif "--progress-now" in sys.argv:
        asyncio.run(run_progress_eval_job())
    else:
        print("Usage: python -m app.scheduler [--run-now | --progress-now]")
