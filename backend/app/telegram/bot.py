import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from app.config import settings

bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher()

_API_BASE = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

_bot_username: str | None = None


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


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Your running coach is ready. Open the app to link Garmin, chat, and track goals.",
        reply_markup=_webapp_keyboard(),
    )


@dp.message(F.text)
async def chat(message: Message) -> None:
    from app.coach.agent import stream_reply

    buffer = ""
    async with httpx.AsyncClient(base_url=_API_BASE, timeout=10) as client:
        async for token in stream_reply(telegram_id=message.from_user.id, text=message.text):
            buffer += token
            await client.post("/sendMessageDraft", json={"chat_id": message.chat.id, "text": buffer})
    await bot.send_message(message.chat.id, buffer or "…")


async def push_message(telegram_id: int, text: str) -> None:
    await bot.send_message(telegram_id, text)
