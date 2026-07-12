import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from garminconnect import Garmin

from app.config import settings

# Pending MFA logins, keyed by telegram_id. Single-process app, so an in-memory
# cache is enough to bridge the two HTTP requests (start -> submit code).
_PENDING_TTL_S = 10 * 60
_pending_logins: dict[int, tuple[Garmin, dict[str, Any], float]] = {}


@dataclass
class LoginOutcome:
    status: str  # "linked" | "mfa_required"


def token_dir_for(telegram_id: int) -> str:
    path = Path(settings.garmin_token_dir) / str(telegram_id)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _prune_pending() -> None:
    now = time.time()
    stale = [tid for tid, (_, _, ts) in _pending_logins.items() if now - ts > _PENDING_TTL_S]
    for tid in stale:
        _pending_logins.pop(tid, None)


async def start_login(telegram_id: int, email: str, password: str) -> LoginOutcome:
    _prune_pending()
    tokenstore = token_dir_for(telegram_id)

    def _do_login() -> tuple[Any, Any]:
        client = Garmin(email=email, password=password, return_on_mfa=True)
        # With return_on_mfa=True the wrapper never auto-persists tokens
        # (that only happens on its non-MFA code path), so we always dump
        # manually below on success.
        result_state, _ = client.login(tokenstore)
        return client, result_state

    client, result_state = await asyncio.to_thread(_do_login)

    if result_state is None:
        # Logged in without MFA.
        await asyncio.to_thread(client.client.dump, tokenstore)
        return LoginOutcome(status="linked")

    _pending_logins[telegram_id] = (client, result_state, time.time())
    return LoginOutcome(status="mfa_required")


async def submit_mfa(telegram_id: int, mfa_code: str) -> LoginOutcome:
    _prune_pending()
    pending = _pending_logins.pop(telegram_id, None)
    if pending is None:
        raise ValueError("no pending login for this user; start over")

    client, client_state, _ = pending
    tokenstore = token_dir_for(telegram_id)

    def _resume() -> None:
        client.resume_login(client_state, mfa_code)
        client.client.dump(tokenstore)  # garth.Client.dump — persist the resumed session

    await asyncio.to_thread(_resume)
    return LoginOutcome(status="linked")


async def get_client(telegram_id: int) -> Garmin:
    client = Garmin()

    def _resume() -> None:
        client.login(token_dir_for(telegram_id))

    await asyncio.to_thread(_resume)
    return client
