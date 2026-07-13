import time
from collections.abc import AsyncIterator
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy import select

from app.config import settings
from app.db.models import User
from app.db.session import async_session
from app.telegram.formatting import has_visible_text, to_telegram_html

bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher()

_bot_username: str | None = None

EDIT_INTERVAL_SECONDS = 1.0

BOT_COMMANDS = [
    ("help", "What this bot does and how to get started"),
    ("sync", "Sync your latest Garmin runs"),
    ("lastworkout", "Analyze your most recent workout"),
    ("weather", "Check the forecast for your saved location"),
    ("location", "Set or check your location for weather"),
    ("recovery", "Check your current recovery status"),
]

HELP_TEXT = (
    "I'm your running coach. I read your Garmin training data and help you plan, "
    "train, and improve.\n\n"
    "First time? Run /start, then tap **Open Coach** to link your Garmin account. "
    "Everything else — chat, /sync, /lastworkout — needs that link first.\n\n"
    "Commands:\n"
    "/sync — pull your latest runs from Garmin\n"
    "/lastworkout — get an analysis of your most recent workout\n"
    "/recovery — check your current recovery status\n"
    "Or just message me anything about your training."
)

LAST_WORKOUT_PROMPT = "Give me a detailed analysis of my most recent workout."

_NOT_LINKED_TEXT = (
    "Garmin isn't linked yet. Run /start, then tap Open Coach to link your account."
)


async def get_bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username
    return _bot_username


def _webapp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Open Coach",
                    web_app=WebAppInfo(url=settings.webapp_url),
                )
            ]
        ]
    )


async def _send_html(chat_id: int, text: str) -> None:
    """Send with HTML formatting, falling back to plain text if the
    conversion produces something Telegram's parser rejects."""
    try:
        await bot.send_message(chat_id, to_telegram_html(text), parse_mode="HTML")
    except TelegramBadRequest:
        await bot.send_message(chat_id, text)


async def _get_user_by_telegram_id(telegram_id: int) -> User | None:
    async with async_session() as db:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()


async def _stream_agent_reply(chat_id: int, token_iter: AsyncIterator[str]) -> None:
    """Send a placeholder, then edit it in place as tokens arrive (throttled
    to Telegram's ~1/sec edit rate limit), converting through to_telegram_html
    on every edit so formatting renders progressively rather than as raw
    markdown."""
    placeholder = await bot.send_message(chat_id, "…")
    buffer = ""
    last_edit = 0.0

    async for token in token_iter:
        buffer += token
        now = time.monotonic()
        if now - last_edit < EDIT_INTERVAL_SECONDS:
            continue
        last_edit = now

        # Partial markdown (e.g. buffer == "#") can convert to tags with no
        # visible text (e.g. "<b></b>") — Telegram rejects that edit as
        # empty outright, so skip this cycle rather than crash the stream.
        converted = to_telegram_html(buffer)
        if not has_visible_text(converted):
            continue
        try:
            await bot.edit_message_text(
                converted,
                chat_id=chat_id,
                message_id=placeholder.message_id,
                parse_mode="HTML",
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

    final_text = buffer or "…"
    converted_final = to_telegram_html(final_text)
    if not has_visible_text(converted_final):
        converted_final = final_text
    try:
        await bot.edit_message_text(
            converted_final,
            chat_id=chat_id,
            message_id=placeholder.message_id,
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        # Formatting slipped through the converter unescaped — fall back to
        # a plain-text edit so the user still gets the full reply.
        await bot.edit_message_text(
            final_text, chat_id=chat_id, message_id=placeholder.message_id
        )


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Your running coach is ready. Open the app to link Garmin, chat, and track goals.",
        reply_markup=_webapp_keyboard(),
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await _send_html(message.chat.id, HELP_TEXT)


@dp.message(Command("sync"))
async def sync_command(message: Message) -> None:
    from app.garmin.sync import sync_day

    user = await _get_user_by_telegram_id(message.from_user.id)
    if user is None or not user.garmin_linked:
        await message.answer(_NOT_LINKED_TEXT)
        return

    await message.answer("Syncing your latest runs…")
    async with async_session() as db:
        db_user = await db.get(User, user.id)
        today = date.today()
        for d in (today, today - timedelta(days=1)):
            await sync_day(db, db_user, d)

    await message.answer("Sync complete.")


@dp.message(Command("lastworkout"))
async def last_workout_command(message: Message) -> None:
    from app.coach.agent import stream_reply

    user = await _get_user_by_telegram_id(message.from_user.id)
    if user is None or not user.garmin_linked:
        await message.answer(_NOT_LINKED_TEXT)
        return

    await _stream_agent_reply(
        message.chat.id,
        stream_reply(telegram_id=message.from_user.id, text=LAST_WORKOUT_PROMPT),
    )


@dp.message(Command("location"))
async def location_command(message: Message) -> None:
    from app.coach.tools import set_location

    city = (message.text or "").partition(" ")[2].strip()
    user = await _get_user_by_telegram_id(message.from_user.id)

    if not city:
        if user and user.location_name:
            await message.answer(
                f"Your location is set to {user.location_name}. Send /location <city> to change it."
            )
        else:
            await message.answer(
                "No location set yet. Send /location <city>, e.g. /location Bucharest."
            )
        return

    async with async_session() as db:
        if user is None:
            user = User(telegram_id=message.from_user.id)
            db.add(user)
            await db.commit()
            await db.refresh(user)
        else:
            user = await db.get(User, user.id)
        result = await set_location(db, user, city)

    if "error" in result:
        await message.answer(
            f'Couldn\'t find a location matching "{city}". Try a different spelling or a nearby larger city.'
        )
        return

    await message.answer(f"Location set to {result['location_name']}.")


@dp.message(Command("weather"))
async def weather_command(message: Message) -> None:
    from app.coach.agent import _format_weather
    from app.coach.tools import get_weather_forecast

    user = await _get_user_by_telegram_id(message.from_user.id)
    if user is None or user.latitude is None:
        await message.answer(
            "No location set yet. Send /location <city> first, e.g. /location Bucharest."
        )
        return

    async with async_session() as db:
        db_user = await db.get(User, user.id)
        forecast = await get_weather_forecast(db, db_user, days=3)

    if "error" in forecast:
        await message.answer(
            "Couldn't fetch the weather right now — try again in a bit."
        )
        return

    await _send_html(
        message.chat.id,
        f"Weather for {forecast['location']}:\n\n" + _format_weather(forecast),
    )


@dp.message(Command("recovery"))
async def recovery_command(message: Message) -> None:
    from app.coach.recovery import compute_recovery_status

    user = await _get_user_by_telegram_id(message.from_user.id)
    if user is None or not user.garmin_linked:
        await message.answer(_NOT_LINKED_TEXT)
        return

    async with async_session() as db:
        db_user = await db.get(User, user.id)
        status = await compute_recovery_status(db, db_user)

    if status is None:
        await message.answer(
            "Not enough recent data to check recovery yet — try /sync first."
        )
        return

    reasons = (
        "; ".join(status.reasons)
        if status.reasons
        else "no specific signal — recovery looks normal"
    )
    await message.answer(
        f"Recovery ({status.metric_date.isoformat()}): {status.level}\n{reasons}"
    )


@dp.message(F.text)
async def chat(message: Message) -> None:
    from app.coach.agent import stream_reply

    await _stream_agent_reply(
        message.chat.id,
        stream_reply(telegram_id=message.from_user.id, text=message.text),
    )


async def push_message(telegram_id: int, text: str) -> None:
    await _send_html(telegram_id, text)


async def register_bot_commands() -> None:
    await bot.set_my_commands(
        [BotCommand(command=cmd, description=desc) for cmd, desc in BOT_COMMANDS]
    )
