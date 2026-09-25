import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.coach.constraints import filter_reasons, suppressed_topics_for
from app.coach.goals import evaluate_goal_achievements, format_achievement_directive
from app.coach.recovery import compute_recovery_status
from app.config import settings
from app.db.models import Activity, DailyMetric, TrainingPlan, User
from app.db.session import async_session
from app.garmin.profile import refresh_profile
from app.garmin.sync import sync_day
from app.weather.client import get_forecast

logger = logging.getLogger(__name__)

# Profile data (zones, VO2max, race predictions, weight) changes slowly; a
# weekly refresh in the nightly job is plenty.
PROFILE_REFRESH_AFTER = timedelta(days=7)
DEFAULT_NOTIFICATION_HOUR = 7

# How often Garmin data is pulled in the background. Garmin itself only
# uploads when the watch syncs, so anything tighter mostly re-reads the same
# numbers; anything looser and the check-in fires on a day with nothing in it.
SYNC_EVERY_HOURS = 2

# An athlete counts as dormant when there is no active plan to report against,
# or nothing logged recently. Daily check-ins have nothing to say to them, so
# they get one gentle nudge a week instead of a push every morning.
DORMANT_AFTER = timedelta(days=10)
NUDGE_EVERY = timedelta(days=7)

# Signals that make a day "live". A row of nothing but nulls (Garmin creates
# one as soon as it is asked about a date) does not count.
_LIVE_SIGNALS = (
    "sleep_score",
    "sleep_duration_s",
    "hrv",
    "resting_hr",
    "stress_avg",
    "body_battery",
    "training_readiness",
)

# Which suppressed topic blanks which field — a field the athlete told the
# coach to ignore can't be the thing that makes their day look live.
_SIGNAL_TOPICS = {
    "sleep_score": "sleep",
    "sleep_duration_s": "sleep",
    "hrv": "hrv",
    "resting_hr": "resting_hr",
    "stress_avg": "stress",
    "body_battery": "body_battery",
    "training_readiness": "training_readiness",
}

scheduler = AsyncIOScheduler(timezone=settings.tz)


def _user_local_now(user: User) -> datetime:
    tz_name = user.timezone or settings.tz
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Invalid timezone %r for user_id=%s — falling back to %s",
            tz_name,
            user.id,
            settings.tz,
        )
        tz = ZoneInfo(settings.tz)
    return datetime.now(tz)


def _notification_hour(user: User) -> int:
    if user.notification_hour is None:
        return DEFAULT_NOTIFICATION_HOUR
    return int(user.notification_hour)


def _local_day_bounds(user: User, day: date) -> tuple[datetime, datetime]:
    """UTC window covering `day` in the user's own timezone."""
    tz = _user_local_now(user).tzinfo
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


async def has_live_data(db: AsyncSession, user: User, day: date, suppressed: set[str]) -> bool:
    """Is there anything from `day` actually worth writing about?

    The check-in used to report on the previous day whatever happened, so a
    morning with no watch data still produced a confident note about numbers
    the athlete had moved on from. A push is only worth sending when today has
    its own data: a metric row with at least one signal the athlete hasn't
    ruled out, or an activity recorded in their local day."""
    metric = (
        await db.execute(
            select(DailyMetric).where(
                DailyMetric.user_id == user.id, DailyMetric.metric_date == day
            )
        )
    ).scalar_one_or_none()
    if metric is not None and any(
        getattr(metric, field) is not None
        for field in _LIVE_SIGNALS
        if _SIGNAL_TOPICS[field] not in suppressed
    ):
        return True

    lo, hi = _local_day_bounds(user, day)
    activity = (
        await db.execute(
            select(Activity.id).where(
                Activity.user_id == user.id,
                Activity.start_time >= lo,
                Activity.start_time < hi,
            )
        )
    ).first()
    return activity is not None


async def is_dormant(db: AsyncSession, user: User) -> bool:
    """No active plan, or nothing trained in DORMANT_AFTER — either way the
    daily check-in would be talking about nothing."""
    plan = (
        await db.execute(
            select(TrainingPlan.id).where(
                TrainingPlan.user_id == user.id, TrainingPlan.status == "active"
            )
        )
    ).first()
    if plan is None:
        return True

    since = datetime.now(timezone.utc) - DORMANT_AFTER
    recent = (
        await db.execute(
            select(Activity.id).where(Activity.user_id == user.id, Activity.start_time >= since)
        )
    ).first()
    return recent is None


async def clear_mute_if_returned(db: AsyncSession, user: User) -> bool:
    """A workout is how the athlete says they're back.

    They muted the coach because they were sick, flat or fed up; nothing else
    should decide when that ends. Training again does — so the first activity
    logged after the mute lifts it, and the nudges pick back up."""
    if user.nudges_muted_at is None:
        return False

    muted_at = user.nudges_muted_at
    if muted_at.tzinfo is None:
        muted_at = muted_at.replace(tzinfo=timezone.utc)

    returned = (
        await db.execute(
            select(Activity.id).where(Activity.user_id == user.id, Activity.start_time > muted_at)
        )
    ).first()
    if returned is None:
        return False

    user.nudges_muted_at = None
    user.nudges_muted_reason = None
    await db.commit()
    logger.info("Nudges un-muted for user_id=%s — they trained again", user.id)
    return True


async def run_user_nudge(user_id: int) -> None:
    """The weekly check-in for a dormant athlete: one short, warm message that
    asks how they are and offers something small — not a training report."""
    from app.coach.agent import generate_progress_summary
    from app.telegram.bot import push_message

    async with async_session() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()

        instruction = (
            "This is a weekly check-in with someone who hasn't trained in a while and has no "
            "active plan. Do not report metrics, do not analyse anything, and do not imply they "
            "have fallen behind or owe you an explanation. Write 1-2 warm, low-pressure sentences "
            "for a push notification: ask how they're doing and offer one small, specific thing "
            "they could do — a short easy run, a walk, a few mobility minutes — sized to what you "
            "know about them. Make it easy to say no to.\n\n"
            "If they reply that they're ill, low, busy or want you to stop messaging, call "
            "pause_check_ins with their reason. Don't push back."
        )
        summary = await generate_progress_summary(db, user, instruction)

        user.last_nudge_at = datetime.now(timezone.utc)
        await db.commit()

    if summary:
        try:
            await push_message(user.telegram_id, summary)
        except Exception:
            logger.exception("Failed to push nudge to telegram_id=%s", user.telegram_id)


async def sync_user_now(user_id: int) -> None:
    """Pull the user's current day (and the one before it in the small hours,
    when last night's sleep is still landing)."""
    async with async_session() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        local = _user_local_now(user)
        await sync_day(db, user, local.date())
        if local.hour < 6:
            await sync_day(db, user, local.date() - timedelta(days=1))


async def run_sync_job() -> None:
    """Background pull for every linked user, every SYNC_EVERY_HOURS hours, so
    the data is already there whenever a check-in or a chat needs it."""
    async with async_session() as db:
        users = list(
            (await db.execute(select(User.id).where(User.garmin_linked.is_(True)))).scalars().all()
        )

    for user_id in users:
        try:
            await sync_user_now(user_id)
        except Exception:
            logger.exception("Background sync failed for user_id=%s", user_id)


async def run_user_progress(user_id: int, day: date) -> None:
    from app.coach.agent import generate_progress_summary
    from app.telegram.bot import push_message

    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()

        await sync_day(db, user, day)

        # Standing instructions decide which signals even count as data.
        suppressed = await suppressed_topics_for(db, user)
        if not await has_live_data(db, user, day, suppressed):
            logger.info(
                "Skipping check-in for user_id=%s — no data for %s yet", user.id, day.isoformat()
            )
            return

        last_profile = user.profile_synced_at
        if last_profile is not None and last_profile.tzinfo is None:
            last_profile = last_profile.replace(tzinfo=timezone.utc)
        if last_profile is None or datetime.now(timezone.utc) - last_profile >= PROFILE_REFRESH_AFTER:
            await refresh_profile(db, user)

        instruction = (
            f"This is the automated daily check-in for {day.isoformat()} — the user's current "
            "day. Base it on that day's data only: do not summarise or re-evaluate earlier days, "
            "and don't comment on a signal that has no reading yet. Compare what is there against "
            "the user's active goals and training plan, and write a short (2-4 sentence) progress "
            "update for them, phrased for a push notification, not a conversation. If something "
            "in the data is worth remembering long-term, call store_memory."
        )

        # Deterministic goal hits — same pattern as recovery flags so the push
        # actually congratulates instead of hoping the LLM notices.
        try:
            hits = await evaluate_goal_achievements(db, user)
        except Exception:
            logger.exception(
                "Goal achievement evaluation failed for user_id=%s", user.id
            )
            hits = []
        if hits:
            instruction = format_achievement_directive(hits) + "\n\n" + instruction

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
            visible = filter_reasons(recovery.reasons, suppressed)
            reasons = "; ".join(visible) if visible else "no specific signal recorded"
            instruction = (
                f"IMPORTANT: recovery signals for {recovery.metric_date.isoformat()} are flagged "
                f"({recovery.level}) — reasons: {reasons}. Lead the message with this and give one "
                "concrete recommendation (easy day, rest, extra sleep) before anything else.\n\n"
            ) + instruction

        # get_forecast never raises (it swallows and logs failures), so this
        # is safe to call unconditionally when a location is set. Uses the
        # user's own timezone (from geocoding) so "upcoming" is their local
        # next day, not UTC's.
        if user.latitude is not None and user.longitude is not None and "weather" not in suppressed:
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
    """Hourly sweep: push users whose local hour matches their notification_hour.

    The day evaluated is the user's own current date. Reporting on yesterday
    made every check-in a day late, and made it fire even when the watch had
    uploaded nothing since."""
    async with async_session() as db:
        result = await db.execute(select(User).where(User.garmin_linked.is_(True)))
        users = list(result.scalars().all())

    now = datetime.now(timezone.utc)
    for user in users:
        try:
            local = _user_local_now(user)
            if local.hour != _notification_hour(user):
                continue

            async with async_session() as db:
                fresh = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
                await clear_mute_if_returned(db, fresh)
                if fresh.nudges_muted_at is not None:
                    continue  # they asked for quiet; only training ends it
                dormant = await is_dormant(db, fresh)
                last_nudge = fresh.last_nudge_at
                if last_nudge is not None and last_nudge.tzinfo is None:
                    last_nudge = last_nudge.replace(tzinfo=timezone.utc)

            if not dormant:
                await run_user_progress(user.id, local.date())
            elif last_nudge is None or now - last_nudge >= NUDGE_EVERY:
                await run_user_nudge(user.id)
        except Exception:
            logger.exception("Daily progress job failed for user_id=%s", user.id)


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


def _progress_instruction(p: dict, suppressed: set[str] | None = None) -> str:
    suppressed = suppressed or set()
    c, d = p["current"], p["deltas"]
    lines = [
        f"This is the {p['period_days']}-day progress check-in. Deterministic stats, "
        f"most recent {p['period_days']} days vs the {p['period_days']} before:",
        f"- Volume: {c['distance_km']} km over {c['runs']} runs "
        f"({_fmt_delta(d['distance_km'], ' km', lower_is_better=False)})",
        f"- Avg pace: {c['avg_pace_s_per_km']}s/km ({_fmt_delta(d['avg_pace_s_per_km'], 's/km', lower_is_better=True)})",
        f"- Avg run HR: {c['avg_hr']} ({_fmt_delta(d['avg_hr'], ' bpm', lower_is_better=True)})",
    ]
    if "resting_hr" not in suppressed:
        lines.append(
            f"- Resting HR: {c['avg_resting_hr']} "
            f"({_fmt_delta(d['avg_resting_hr'], ' bpm', lower_is_better=True)})"
        )
    if "vo2max" not in suppressed:
        lines.append(f"- VO2max: {c['vo2max']} ({_fmt_delta(d['vo2max'], '', lower_is_better=False)})")
    # Cross-training counts as training: a fortnight of gym work is a different
    # story from a fortnight off, and the note should say so.
    if c.get("other_sessions") or c.get("strength_sessions"):
        mix = ", ".join(
            f"{count}x {sport}" for sport, count in sorted(c.get("sessions_by_sport", {}).items())
        )
        lines.append(
            f"- Cross-training: {c['strength_sessions']} strength, {c['other_sessions']} non-run "
            f"sessions ({_fmt_delta(d.get('other_sessions'), '', lower_is_better=False)}) — {mix}"
        )
    if c.get("total_training_time_s"):
        lines.append(f"- Total training time: {round(c['total_training_time_s'] / 3600, 1)}h")
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
        await clear_mute_if_returned(db, user)
        if user.nudges_muted_at is not None:
            return  # asked for quiet — that covers every automated push
        progress = await compute_progress(db, user)
        if progress is None:
            return  # no training recorded yet — try again next cycle

        instruction = _progress_instruction(progress, await suppressed_topics_for(db, user))
        summary = await generate_progress_summary(db, user, instruction)

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
        run_sync_job,
        CronTrigger(hour=f"*/{SYNC_EVERY_HOURS}", minute=30),
        id="garmin_sync",
        replace_existing=True,
    )
    scheduler.add_job(
        run_daily_progress_job,
        CronTrigger(minute=0),
        id="hourly_progress",
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
    elif "--sync-now" in sys.argv:
        asyncio.run(run_sync_job())
    elif "--progress-now" in sys.argv:
        asyncio.run(run_progress_eval_job())
    else:
        print("Usage: python -m app.scheduler [--run-now | --sync-now | --progress-now]")
