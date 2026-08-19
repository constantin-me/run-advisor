"""Test env bootstrap.

app.config.Settings reads required values at import time, and app.garmin
imports it transitively. Tests here are pure builder tests — no DB, no network,
no Garmin login — so dummy values are enough and keep the suite runnable
without a .env.
"""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("WEBAPP_URL", "http://localhost")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test/test")
os.environ.setdefault("GARMIN_TOKEN_DIR", "/tmp/garmin-tokens-test")
