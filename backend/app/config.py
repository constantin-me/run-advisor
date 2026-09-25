from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    webapp_url: str

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str
    llm_model: str = "gpt-4o"
    # Some reasoning models reject function tools unless this is set to
    # "none" on the chat/completions endpoint. Leave unset for models/
    # providers that don't recognize the field.
    llm_reasoning_effort: str | None = None
    # Small, cheap model for lane classification and the read-only lanes
    # (quick answers, small talk). Falls back to llm_model when unset.
    llm_router_model: str | None = None
    # Multimodal model used when the athlete sends a photo. Falls back to
    # llm_model, which is fine when that model already takes images.
    llm_vision_model: str | None = None

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 60 * 60 * 24 * 7

    database_url: str

    garmin_token_dir: str = "/data/garmin_tokens"

    tz: str = "UTC"

    # Lets the webapp be used in a plain browser (no Telegram initData) via
    # /api/auth/dev-session. Must stay off outside local development.
    dev_mode: bool = False


settings = Settings()
