# Garmin Running Coach

Telegram Mini App that connects to Garmin Connect, learns your training/health data, and coaches your running via chat, goals, and generated training plans. See `MASTER.md` for the full plan.

## Dev setup

1. `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`, `JWT_SECRET`.
2. Expose the app over HTTPS for Telegram Mini Apps in dev, e.g.:
   ```
   cloudflared tunnel --url http://localhost:8010
   ```
   Put the resulting URL into `WEBAPP_URL` in `.env`, and set it as your bot's Mini App URL via @BotFather.
3. `docker compose up --build`
4. Run migrations: `docker compose exec backend alembic upgrade head`
5. Open the bot in Telegram, `/start`, tap **Open Coach**.

## Layout

- `backend/` — FastAPI + aiogram, single Python process (REST/SSE API, Telegram bot, scheduler).
- `webapp/` — Vite + React Mini App, built into `backend/static` at image build time.
- `docker-compose.yml` — backend, postgres.
- `deploy/` — DigitalOcean droplet provisioning script + Caddy config for a real HTTPS deployment. See `deploy/README.md`.
