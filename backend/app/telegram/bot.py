import base64
import logging
import time
from collections.abc import AsyncIterator

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
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

logger = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher()

_bot_username: str | None = None

EDIT_INTERVAL_SECONDS = 1.0
# After a bare /location, treat the next text message as the city for this long.
_LOCATION_PENDING_TTL_S = 120.0
# telegram_id -> monotonic deadline
_pending_location: dict[int, float] = {}

BOT_COMMANDS = [
    ("help", "What this bot does and how to get started"),
    ("weather", "Check the forecast for your saved location"),
    ("location", "Set or check your location for weather"),
    ("recovery", "Check your current recovery status"),
]

HELP_TEXT = (
    "I'm your running coach. I read your Garmin training data and help you plan, "
    "train, and improve.\n\n"
    "First time? Run /start, then tap **Open Coach** to link your Garmin account. "
    "Your history syncs automatically once linked, and stays fresh on its own.\n\n"
    "Commands:\n"
    "/recovery — check your current recovery status\n"
    "/weather — forecast for your location\n"
    "/location <city> — set your location, e.g. /location Bucharest\n"
    "Or just message me anything about your training."
)

_NOT_LINKED_TEXT = (
    "Garmin isn't linked yet. Run /start, then tap Open Coach to link your account."
)


def _set_pending_location(telegram_id: int) -> None:
    _pending_location[telegram_id] = time.monotonic() + _LOCATION_PENDING_TTL_S


def _pop_pending_location(telegram_id: int) -> bool:
    deadline = _pending_location.pop(telegram_id, None)
    if deadline is None:
        return False
    return time.monotonic() <= deadline


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


async def _apply_location(message: Message, city: str) -> None:
    from app.coach.tools import set_location

    user = await _get_user_by_telegram_id(message.from_user.id)
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


@dp.message(Command("location"))
async def location_command(message: Message, command: CommandObject) -> None:
    city = (command.args or "").strip()
    user = await _get_user_by_telegram_id(message.from_user.id)

    if not city:
        _set_pending_location(message.from_user.id)
        if user and user.location_name:
            await message.answer(
                f"Your location is set to {user.location_name}. "
                "Send a city name, or /location <city> (e.g. /location Bucharest)."
            )
        else:
            await message.answer(
                "No location set yet. Send a city name, or /location <city>, "
                "e.g. /location Bucharest."
            )
        return

    _pending_location.pop(message.from_user.id, None)
    await _apply_location(message, city)


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
            "Not enough recent data to check recovery yet — it'll fill in as your Garmin data syncs."
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

    telegram_id = message.from_user.id
    if _pop_pending_location(telegram_id):
        city = (message.text or "").strip()
        if city and not city.startswith("/"):
            await _apply_location(message, city)
            return

    await _stream_agent_reply(
        message.chat.id,
        stream_reply(telegram_id=telegram_id, text=message.text),
    )


# Telegram serves several sizes of every photo; the largest is the last. Cap
# what we forward to the model — a 4MB upload costs tokens and adds latency
# without making a whiteboard any more readable.
MAX_PHOTO_BYTES = 4 * 1024 * 1024


@dp.message(F.photo)
async def photo(message: Message) -> None:
    """A photo of a workout — a whiteboard, a screenshot, a watch face — goes to
    the vision lane, with the caption as the question."""
    from app.coach.agent import stream_reply

    largest = message.photo[-1]
    if largest.file_size and largest.file_size > MAX_PHOTO_BYTES:
        await _send_html(message.chat.id, "That image is too large for me — send a smaller one?")
        return

    try:
        buffer = await bot.download(largest)
        raw = buffer.read()
    except Exception:
        logger.exception("Failed to download photo from telegram_id=%s", message.from_user.id)
        await _send_html(message.chat.id, "I couldn't open that image. Mind sending it again?")
        return

    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    await _stream_agent_reply(
        message.chat.id,
        stream_reply(
            telegram_id=message.from_user.id,
            text=(message.caption or "").strip(),
            image_data_url=data_url,
        ),
    )


async def push_message(telegram_id: int, text: str) -> None:
    await _send_html(telegram_id, text)


async def register_bot_commands() -> None:
    await bot.set_my_commands(
        [BotCommand(command=cmd, description=desc) for cmd, desc in BOT_COMMANDS]
    )
