import hashlib
import hmac
import time
from urllib.parse import parse_qsl

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import User
from app.db.session import get_db

INIT_DATA_MAX_AGE_S = 24 * 60 * 60


class InvalidInitData(Exception):
    pass


def validate_init_data(init_data: str) -> dict:
    """Validate Telegram Mini App initData per Telegram's HMAC scheme and
    return the parsed key/value pairs (including the nested `user` JSON as a raw string)."""
    pairs = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InvalidInitData("missing hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise InvalidInitData("hash mismatch")

    auth_date = int(pairs.get("auth_date", 0))
    if time.time() - auth_date > INIT_DATA_MAX_AGE_S:
        raise InvalidInitData("expired")

    return pairs


def create_session_token(telegram_id: int) -> str:
    payload = {"sub": str(telegram_id), "exp": int(time.time()) + settings.jwt_ttl_seconds}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_session_token(token: str) -> int:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    return int(payload["sub"])


async def get_current_user(
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    telegram_id = decode_session_token(authorization.removeprefix("Bearer "))

    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="unknown user")
    return user
