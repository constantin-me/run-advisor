import json
import logging
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta

from openai import AsyncOpenAI, OpenAIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.coach.constraints import (
    filter_reasons,
    format_constraint_block,
    load_constraints,
    suppressed_topics,
)
from app.coach.goals import evaluate_goal_achievements, format_achievement_directive
from app.coach.prompts import SYSTEM_PROMPT
from app.coach.recovery import compute_recovery_status
from app.coach.router import Lane, classify, lane
from app.coach.tools import (
    TOOL_SCHEMAS,
    _format_hour_label,
    execute_tool,
    get_daily_metrics,
    get_goals,
    get_training_plan,
    get_weather_forecast,
)
from app.config import settings
from app.db.models import ChatMessage, User
from app.db.session import async_session
from app.garmin.sync import refresh_if_stale
from app.memory.client import list_recent_memories

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)

HISTORY_MESSAGES = 20
# Saving a plan now costs extra rounds: save -> read the verification report ->
# correct -> verify again. Too low a ceiling would cut the coach off mid-fix.
MAX_TOOL_ROUNDS = 8


async def _load_history(db: AsyncSession, user: User) -> list[dict]:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.desc())
        .limit(HISTORY_MESSAGES)
    )
    rows = list(result.scalars().all())[::-1]
    return [{"role": r.role, "content": r.content} for r in rows]


def _format_metrics(metrics: list[dict]) -> str:
    lines = []
    for m in metrics[:7]:
        parts = [f"sleep {m['sleep_score']}" if m["sleep_score"] is not None else None]
        parts.append(f"HRV {m['hrv']}" if m["hrv"] is not None else None)
        parts.append(f"RHR {m['resting_hr']}" if m["resting_hr"] is not None else None)
        parts.append(
            f"stress {m['stress_avg']}" if m["stress_avg"] is not None else None
        )
        parts.append(
            f"body battery {m['body_battery']}"
            if m["body_battery"] is not None
            else None
        )
        detail = ", ".join(p for p in parts if p) or "no data"
        lines.append(f"- {m['date']}: {detail}")
    return "\n".join(lines)


def _fmt_hms(seconds) -> str | None:
    if not seconds:
        return None
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _format_profile(profile: dict) -> str:
    """Readable athlete profile block for the system prompt — the foundational
    facts (fitness, physiology, configured zones) the coach should calibrate
    all advice against."""
    parts: list[str] = []
    if profile.get("vo2max"):
        parts.append(f"VO2max {profile['vo2max']:.0f}")
    hr = profile.get("hr") or {}
    if hr.get("max_hr"):
        floors = hr.get("zone_floors")
        zones = f" (zone floors {'/'.join(str(f) for f in floors)})" if floors else ""
        parts.append(f"max HR {hr['max_hr']}{zones}")
    lthr = profile.get("lactate_threshold_hr") or hr.get("lactate_threshold_hr")
    if lthr:
        parts.append(f"lactate threshold HR {lthr}")
    if profile.get("resting_hr"):
        parts.append(f"resting HR {profile['resting_hr']}")
    if profile.get("weight_kg"):
        parts.append(f"weight {profile['weight_kg']} kg")
    if profile.get("chronological_age"):
        parts.append(f"age {profile['chronological_age']}")

    lines = []
    if parts:
        lines.append("Athlete profile: " + ", ".join(parts) + ".")

    races = profile.get("race_predictions_s") or {}
    race_bits = []
    for key, label in (("5k", "5K"), ("10k", "10K"), ("half", "HM"), ("marathon", "M")):
        t = _fmt_hms(races.get(key))
        if t:
            race_bits.append(f"{label} {t}")
    if race_bits:
        lines.append("Garmin race predictions: " + ", ".join(race_bits) + ".")

    return "\n".join(lines)


def _format_recovery(status, suppressed: set[str] | None = None) -> str:
    today = date.today()
    if status.metric_date == today:
        when = "today's"
    elif status.metric_date == today - timedelta(days=1):
        when = "yesterday's"
    else:
        when = f"{status.metric_date.isoformat()}'s"
    visible = filter_reasons(status.reasons, suppressed or set())
    reasons = "; ".join(visible) if visible else "no specific signal recorded"
    return f"Recovery flag ({when} data): {status.level} — {reasons}"


def _format_weather(forecast: dict) -> str:
    windows_by_date = forecast.get("windows", {})
    lines = []
    for day in forecast["daily"][:2]:
        label = datetime.strptime(day["date"], "%Y-%m-%d").strftime("%d %b")
        line = (
            f"{label}: {day['temp_min']:.0f}-{day['temp_max']:.0f}°C "
            f"(feels like up to {day['feels_like_max']:.0f}°C), "
            f"{day['precipitation_probability']}% rain, wind {day['wind_speed_max']:.0f}km/h ({day['condition']})"
        )
        window = windows_by_date.get(day["date"])
        if window:
            parts = [
                f"{name} {window[name]['temp']:.0f}°C"
                for name in ("morning", "midday", "evening")
                if name in window
            ]
            if parts:
                line += "; " + " / ".join(parts)
        lines.append(line)
    return "\n".join(lines)


async def _build_system_prompt(db: AsyncSession, user: User) -> str:
    """Always-on context so the coach can answer grounded questions (e.g.
    "what do you know about me?") without depending on the model deciding
    to call a tool first."""
    system_prompt = SYSTEM_PROMPT

    # Standing instructions come first and in full: they change how everything
    # below is allowed to be used.
    constraints = await load_constraints(db, user)
    suppressed = suppressed_topics([c["text"] for c in constraints])
    block = format_constraint_block(constraints, suppressed)
    if block:
        system_prompt += "\n\n" + block

    memories = await list_recent_memories(user.telegram_id)
    if memories:
        system_prompt += "\n\nKnown facts about this user:\n" + "\n".join(
            f"- {m}" for m in memories
        )

    if user.garmin_profile:
        profile_block = _format_profile(user.garmin_profile)
        if profile_block:
            system_prompt += "\n\n" + profile_block
        if not user.garmin_profile.get("weight_kg") and "weight" not in suppressed:
            system_prompt += (
                "\n\nWeight is unknown (not on file in Garmin). If it becomes relevant to the "
                "user's question (e.g. fueling, load), ask them for it once, then store_memory it."
            )

    metrics = await get_daily_metrics(db, user, days=7)
    if metrics:
        system_prompt += (
            "\n\nRecent health data (last 7 days, most recent first):\n"
            + _format_metrics(metrics)
        )
    else:
        system_prompt += "\n\nNo Garmin health data synced yet."

    try:
        recovery = await compute_recovery_status(db, user)
        if recovery is not None and recovery.level != "green":
            system_prompt += "\n\n" + _format_recovery(recovery, suppressed)
    except Exception:
        # Never let a bug in the recovery rule engine break chat itself.
        logger.exception("Recovery status computation failed for user_id=%s", user.id)

    goals = await get_goals(db, user)
    if goals:
        goal_lines = "\n".join(
            f"- id={g['id']} {g['text']}"
            + (f" (by {g['target_date']})" if g["target_date"] else "")
            for g in goals
        )
        system_prompt += "\n\nActive goals:\n" + goal_lines
    else:
        system_prompt += "\n\nNo active goals set."

    try:
        hits = await evaluate_goal_achievements(db, user)
        if hits:
            system_prompt += "\n\n" + format_achievement_directive(hits)
    except Exception:
        logger.exception(
            "Goal achievement evaluation failed for user_id=%s", user.id
        )

    hour = user.notification_hour if user.notification_hour is not None else 7
    tz = user.timezone or settings.tz
    system_prompt += (
        f"\n\nDaily check-in push: {_format_hour_label(hour)} local "
        f"({tz}). Use set_checkin_time if the user wants a different whole hour."
    )

    plan = await get_training_plan(db, user)
    if plan:
        system_prompt += f"\n\nActive training plan: \"{plan['title']}\" ({len(plan['workouts'])} scheduled workouts)."
    else:
        system_prompt += "\n\nNo active training plan."

    if user.latitude is not None and "weather" not in suppressed:
        try:
            forecast = await get_weather_forecast(db, user, days=2)
            if "error" not in forecast:
                system_prompt += (
                    "\n\nWeather forecast (next 2 days, local time):\n"
                    + _format_weather(forecast)
                )
        except Exception:
            # Weather is a nice-to-have, not load-bearing — never let a
            # forecast fetch problem break the whole chat turn.
            logger.exception("Weather forecast fetch failed for user_id=%s", user.id)

    return system_prompt


def _tools_for(active: Lane) -> list[dict]:
    if active.tools is None:
        return TOOL_SCHEMAS
    allowed = set(active.tools)
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] in allowed]


async def _run_tool_loop(
    db: AsyncSession, user: User, messages: list[dict], active: Lane | None = None
) -> AsyncGenerator[str, None]:
    """Drive the OpenAI-compatible tool-calling loop over an already-built
    message list, yielding content tokens as they stream. Does not touch
    chat_messages — callers decide what (if anything) to persist.

    The lane decides which model runs and which tools it can see; without one
    this is the full coach on every tool, which is what the scheduled jobs
    want."""
    active = active or lane("coach")
    tools = _tools_for(active)
    for _ in range(MAX_TOOL_ROUNDS):
        content_acc = ""
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = None

        create_kwargs = {}
        if settings.llm_reasoning_effort:
            create_kwargs["reasoning_effort"] = settings.llm_reasoning_effort

        try:
            stream = await _client.chat.completions.create(
                model=active.model,
                messages=messages,
                tools=tools,
                stream=True,
                **create_kwargs,
            )
            async for chunk in stream:
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.content:
                    content_acc += delta.content
                    yield delta.content
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        acc = tool_calls_acc.setdefault(
                            tc.index, {"id": None, "name": None, "arguments": ""}
                        )
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function and tc.function.name:
                            acc["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            acc["arguments"] += tc.function.arguments
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        except OpenAIError:
            logger.exception("LLM call failed")
            if not content_acc:
                yield "Sorry, I hit an error talking to the language model. Please try again."
            return

        if finish_reason != "tool_calls" or not tool_calls_acc:
            break

        assistant_tool_calls = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            }
            for tc in tool_calls_acc.values()
        ]
        messages.append(
            {
                "role": "assistant",
                "content": content_acc or None,
                "tool_calls": assistant_tool_calls,
            }
        )

        for tc in tool_calls_acc.values():
            try:
                args = json.loads(tc["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = await execute_tool(db, user, tc["name"], args)
            except Exception as exc:
                logger.exception("Tool %s failed", tc["name"])
                result = {"error": str(exc)}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, default=str),
                }
            )
    else:
        yield "I ran out of tool-call rounds — try rephrasing your question."


def _user_turn(text: str, image_data_url: str | None) -> dict:
    """The outgoing user message. With an image it becomes the multimodal
    content-parts form every OpenAI-compatible vision endpoint takes."""
    if image_data_url is None:
        return {"role": "user", "content": text}
    parts: list[dict] = [{"type": "image_url", "image_url": {"url": image_data_url}}]
    if text:
        parts.insert(0, {"type": "text", "text": text})
    return {"role": "user", "content": parts}


async def stream_chat(
    db: AsyncSession,
    user: User,
    user_message: str,
    image_data_url: str | None = None,
) -> AsyncGenerator[str, None]:
    # Stored history is text: an image is recorded as a marker plus whatever
    # they wrote with it, so later turns know a photo was part of the thread
    # without carrying its bytes around forever.
    stored = user_message
    if image_data_url is not None:
        stored = f"[sent a photo] {user_message}".strip()
    db.add(ChatMessage(user_id=user.id, role="user", content=stored))
    await db.commit()

    # Ground the reply in near-current data: if the last Garmin sync is older
    # than the staleness window, refresh today's data first. Self-throttling
    # (once per window) and failure-safe (a Garmin outage never blocks chat).
    await refresh_if_stale(db, user)

    final_text = ""
    try:
        active = lane(await classify(user_message, has_image=image_data_url is not None))
        logger.info("Routing user_id=%s to lane=%s (%s)", user.id, active.name, active.model)

        history = await _load_history(db, user)
        system_prompt = await _build_system_prompt(db, user) + active.prompt_suffix
        messages: list[dict] = [{"role": "system", "content": system_prompt}, *history]
        if image_data_url is not None:
            # The stored history line for this turn is only the text marker, so
            # the picture itself is attached here.
            messages[-1] = _user_turn(user_message, image_data_url)

        async for token in _run_tool_loop(db, user, messages, active):
            final_text += token
            yield token
    except Exception:
        # Anything unexpected here (not just LLM-call failures, which
        # _run_tool_loop already handles) must not crash the streaming
        # ASGI response mid-flight — surface it instead of dying silently.
        logger.exception("stream_chat failed for user_id=%s", user.id)
        if not final_text:
            final_text = "Sorry, something went wrong on my end. Please try again."
            yield final_text

    if final_text:
        db.add(ChatMessage(user_id=user.id, role="assistant", content=final_text))
        await db.commit()


async def generate_progress_summary(
    db: AsyncSession, user: User, instruction: str
) -> str:
    """Non-interactive coach pass used by the daily check-in job. Does not touch
    chat_messages — the result is pushed via Telegram, not shown as a turn
    in the visible conversation."""
    try:
        system_prompt = await _build_system_prompt(db, user)
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": instruction},
        ]

        final_text = ""
        async for token in _run_tool_loop(db, user, messages):
            final_text += token
        return final_text
    except Exception:
        logger.exception("generate_progress_summary failed for user_id=%s", user.id)
        return ""


async def stream_reply(
    telegram_id: int, text: str, image_data_url: str | None = None
) -> AsyncGenerator[str, None]:
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id)
            db.add(user)
            await db.commit()
            await db.refresh(user)

        async for token in stream_chat(db, user, text, image_data_url):
            yield token
