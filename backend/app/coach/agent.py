import json
import logging
from collections.abc import AsyncGenerator

from openai import AsyncOpenAI, OpenAIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.coach.prompts import SYSTEM_PROMPT
from app.coach.tools import TOOL_SCHEMAS, execute_tool, get_daily_metrics, get_goals, get_training_plan
from app.config import settings
from app.db.models import ChatMessage, User
from app.db.session import async_session
from app.memory.client import list_recent_memories

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)

HISTORY_MESSAGES = 20
MAX_TOOL_ROUNDS = 5


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
        parts.append(f"stress {m['stress_avg']}" if m["stress_avg"] is not None else None)
        parts.append(f"body battery {m['body_battery']}" if m["body_battery"] is not None else None)
        detail = ", ".join(p for p in parts if p) or "no data"
        lines.append(f"- {m['date']}: {detail}")
    return "\n".join(lines)


async def _build_system_prompt(db: AsyncSession, user: User) -> str:
    """Always-on context so the coach can answer grounded questions (e.g.
    "what do you know about me?") without depending on the model deciding
    to call a tool first."""
    system_prompt = SYSTEM_PROMPT

    memories = await list_recent_memories(user.telegram_id)
    if memories:
        system_prompt += "\n\nKnown facts about this user:\n" + "\n".join(f"- {m}" for m in memories)

    metrics = await get_daily_metrics(db, user, days=7)
    if metrics:
        system_prompt += "\n\nRecent health data (last 7 days, most recent first):\n" + _format_metrics(metrics)
    else:
        system_prompt += "\n\nNo Garmin health data synced yet."

    goals = await get_goals(db, user)
    if goals:
        goal_lines = "\n".join(f"- {g['text']}" + (f" (by {g['target_date']})" if g["target_date"] else "") for g in goals)
        system_prompt += "\n\nActive goals:\n" + goal_lines
    else:
        system_prompt += "\n\nNo active goals set."

    plan = await get_training_plan(db, user)
    if plan:
        system_prompt += f"\n\nActive training plan: \"{plan['title']}\" ({len(plan['workouts'])} scheduled workouts)."
    else:
        system_prompt += "\n\nNo active training plan."

    return system_prompt


async def _run_tool_loop(db: AsyncSession, user: User, messages: list[dict]) -> AsyncGenerator[str, None]:
    """Drive the OpenAI-compatible tool-calling loop over an already-built
    message list, yielding content tokens as they stream. Does not touch
    chat_messages — callers decide what (if anything) to persist."""
    for _ in range(MAX_TOOL_ROUNDS):
        content_acc = ""
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = None

        create_kwargs = {}
        if settings.llm_reasoning_effort:
            create_kwargs["reasoning_effort"] = settings.llm_reasoning_effort

        try:
            stream = await _client.chat.completions.create(
                model=settings.llm_model,
                messages=messages,
                tools=TOOL_SCHEMAS,
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
                        acc = tool_calls_acc.setdefault(tc.index, {"id": None, "name": None, "arguments": ""})
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
        messages.append({"role": "assistant", "content": content_acc or None, "tool_calls": assistant_tool_calls})

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
                {"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, default=str)}
            )
    else:
        yield "I ran out of tool-call rounds — try rephrasing your question."


async def stream_chat(db: AsyncSession, user: User, user_message: str) -> AsyncGenerator[str, None]:
    db.add(ChatMessage(user_id=user.id, role="user", content=user_message))
    await db.commit()

    final_text = ""
    try:
        history = await _load_history(db, user)
        system_prompt = await _build_system_prompt(db, user)
        messages: list[dict] = [{"role": "system", "content": system_prompt}, *history]

        async for token in _run_tool_loop(db, user, messages):
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


async def generate_progress_summary(db: AsyncSession, user: User, instruction: str) -> str:
    """Non-interactive coach pass used by the midnight job. Does not touch
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


async def stream_reply(telegram_id: int, text: str) -> AsyncGenerator[str, None]:
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id)
            db.add(user)
            await db.commit()
            await db.refresh(user)

        async for token in stream_chat(db, user, text):
            yield token
