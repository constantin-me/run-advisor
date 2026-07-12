# Garmin Running Coach — Telegram Mini App

## Context

Standalone Telegram Mini App (web app) that connects to the user's Garmin account, syncs health/training data daily, remembers goals and history per user, and acts as an AI running coach: analyzes trends, prepares training plans, and answers questions with streamed replies. Greenfield project in `/Users/constantinmelniciuc/Documents/Projects/garmin-connector` (empty dir, no git yet).

**Confirmed decisions:**
- Python backend · unofficial Garmin API (`python-garminconnect`) · Docker Compose · Postgres for both structured data and agent memory (see note below).
- **Telegram Mini App**, not command-driven bot — a tiny web app inside Telegram, portable to plain web later.
- **Cloud LLM via OpenAI-compatible API** (OpenAI / Qwen via DashScope / GLM via z.ai — user picks later; must be swappable via env: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`).
- Bot-chat path uses native Telegram streaming: **`sendMessageDraft`** (Bot API ≥9.0) for progressive display of LLM output.

## Architecture

```
┌────────────────────────────┐
│ Telegram client             │
│  ├─ Mini App (webapp SPA)───┼──HTTPS──┐
│  └─ bot chat (drafts)       │         │
└──────────────┬─────────────┘         ▼
               │ Bot API      ┌─────────────────────────────┐
               └─────────────►│ backend (Python, FastAPI)    │
                              │  ├─ REST + SSE chat stream   │
                              │  ├─ aiogram bot (same loop)  │
                              │  ├─ coach agent (openai SDK) │──► cloud LLM
                              │  ├─ garmin sync              │    (OpenAI-compatible)
                              │  └─ APScheduler midnight job │
                              └──────────────┬──────────────┘
                                             ▼
                                      ┌──────────┐
                                      │ postgres  │
                                      │  :5432    │
                                      └──────────┘
```

- **One Python process**: FastAPI (uvicorn) + aiogram polling share the asyncio loop. Compose services: `backend`, `postgres`.
- **Frontend**: Vite + React (TypeScript) SPA in `webapp/`, built statics served by FastAPI. Opened via bot's Mini App button; later deployable as a standalone website (auth is the only Telegram-specific seam).
- **Auth**: Mini App sends Telegram `initData` → backend validates HMAC against bot token → issues short-lived session JWT. Auth logic isolated in one module so a web login can replace it later. Local dev without Telegram: `DEV_MODE=true` enables `POST /api/auth/dev-session`, which issues a session for a fixed dev user (`telegram_id=-1`) with no initData needed — the webapp falls back to it automatically when `window.Telegram` isn't present. Must be off outside local dev.
- **SPA routing**: the backend serves the built webapp with a catch-all fallback to `index.html` (path-traversal-guarded) so deep links / refreshes on `/goals`, `/plan`, `/chat` work, not just `/`.
- **Mini App needs a public HTTPS URL** — dev via cloudflared/ngrok tunnel, documented in README.

## Two chat surfaces, one agent

1. **Mini App chat (primary)**: SPA ↔ backend over SSE — true token streaming into the app UI.
2. **Bot DM chat (secondary)**: free text to the bot → same agent → progressive output via `sendMessageDraft`, final `sendMessage` when generation completes. If aiogram lacks the method, call it via raw Bot API request.

Both funnel into the same `coach/agent.py`.

## Data ownership split

- **Postgres = authoritative structured data**: users, Garmin tokens, activities, daily metrics, goals, training plans.
- **Postgres `memories` table = conversational/semantic memory** (injury history, preferences, reactions to advice), scoped by `user_id` FK, searched via full-text (`to_tsvector`/`plainto_tsquery`) and recalled into the system prompt every conversation. Originally planned to use [agentmemory](https://github.com/rohitg00/agentmemory), but live testing found its `project` scoping field is silently ignored by `/smart-search` — a query under one user's project returned another user's stored memories. That's a data leak for a personal-health bot, so memory moved into Postgres instead, using the plan's own documented fallback (see Risks).

## Project layout

```
garmin-connector/
├── docker-compose.yml
├── .env.example            # TELEGRAM_BOT_TOKEN, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, DB creds, WEBAPP_URL
├── README.md
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml       # fastapi, uvicorn, aiogram, garminconnect, openai, sqlalchemy[asyncio], asyncpg, apscheduler, httpx, alembic
│   └── app/
│       ├── main.py          # FastAPI app + aiogram startup + scheduler
│       ├── config.py        # pydantic-settings
│       ├── auth.py          # initData HMAC validation → JWT (the portability seam)
│       ├── api/             # routes: chat (SSE), metrics, goals, plans, garmin link/sync
│       ├── db/              # models.py, session.py, alembic/
│       ├── garmin/          # client.py (garth token persistence), sync.py
│       ├── coach/           # agent.py (openai SDK tool loop, async generator of tokens), tools.py, prompts.py
│       ├── memory/client.py # Postgres full-text search over the `memories` table, scoped by user_id
│       ├── telegram/        # bot.py (Mini App button, DM chat via sendMessageDraft, push notifications)
│       └── scheduler.py     # midnight sync + progress push
└── webapp/
    ├── Dockerfile (build stage only; statics copied into backend image)
    ├── package.json          # react, vite, @telegram-apps/sdk
    └── src/
        ├── pages: Chat, Dashboard (metrics charts), Goals, Plan
        └── lib/api.ts        # fetch + SSE client, initData header
```

## Key components

### 1. Garmin sync (`garmin/`)
- `garminconnect` + garth; tokens persisted to a volume (~1 yr validity). Login survives restarts.
- Linking flow in Mini App: settings page → email/password form (HTTPS, never stored — only garth tokens kept). Handle Garmin MFA prompt.
- `sync.py` pulls per day: activities (pace, distance, HR, cadence), sleep, HRV, resting HR, stress, body battery, VO2max, training status/readiness. Raw JSON + normalized columns; idempotent upserts keyed on (user, date) / activity id.
- `/api/garmin/sync` = quick sync (today + yesterday). `/api/garmin/backfill` = bulk history: one `get_activities(limit=200)` call for activity history (fast) plus per-day metrics for the last N days (default 30, capped at 365 — this part is slow, one Garmin call set per day). Verified live against a real account: 192 activities + 30 days of metrics backfilled correctly. Garmin's two activity endpoints disagree on timestamp format (`get_activities_fordate` → `"2026-06-09 05:00:49"`, `get_activities` → ISO `"2026-06-21T12:10:13.0"`) — `_parse_garmin_datetime` tries both.

### 2. Database (Postgres, SQLAlchemy async + Alembic)
`users` (telegram_id, garmin linked flag) · `activities` · `daily_metrics` (user+date: sleep score, hrv, rhr, stress, body battery, vo2max…) · `goals` (text, target date, status) · `training_plans` + `plan_workouts` (week/day, type, target pace/distance, done flag) · `chat_messages` (history for the Mini App UI) · `memories` (user_id, content, created_at — full-text searched).

### 3. Coach agent (`coach/`)
- `openai` Python SDK pointed at `LLM_BASE_URL` — identical code for OpenAI, Qwen (DashScope compatible-mode), GLM (z.ai). Streaming chat completions + OpenAI tool-calling spec (reliable, unlike local models).
- Agent loop: recall memories → system prompt (recent metrics summary + memories + active goal/plan) → stream; execute tool calls, continue until final answer. Exposed as an async token generator consumed by both SSE route and bot-draft sender.
- Tools: `get_daily_metrics(days)`, `get_recent_activities(n)`, `get_goals`/`save_goal`, `get_training_plan`/`save_training_plan`, `search_memory(query)`, `store_memory(fact)`.

### 4. Mini App (webapp/)
- Pages: **Chat** (streamed coach conversation), **Dashboard** (7/30-day metric charts, recent runs), **Goals** (list/add), **Plan** (weekly workout table, mark done).
- `@telegram-apps/sdk` for initData, theme, back button. All Telegram-specific bits behind a small adapter → plain-web port later = swap auth + adapter.

### 5. Telegram bot layer (minimal)
- `/start` → button opening the Mini App (`web_app` keyboard).
- Free-text DM → coach agent, streamed with `sendMessageDraft`.
- Outbound push: midnight progress message + workout reminders.

### 6. Midnight job (`scheduler.py`)
- APScheduler cron `0 0 * * *` (TZ from env): per linked user → sync yesterday → non-interactive coach pass vs. goals/plan → store summary in `memories` → push short progress message via bot.

## Implementation order

1. **Scaffold**: compose (backend+postgres), FastAPI skeleton, DB models + first migration, bot `/start` with Mini App button, Vite app served — "hello" end-to-end through tunnel.
2. **Auth**: initData validation → JWT; wire webapp API client.
3. **Garmin**: link form in Mini App, token persistence, `sync.py`, manual sync button storing real data; Dashboard page reads metrics.
4. **Coach chat**: agent with OpenAI-compatible streaming + tools; SSE into Mini App Chat page; bot DM path via `sendMessageDraft`.
5. **Memory**: Postgres `memories` table + full-text search client, recall/store wired into agent (see Data ownership split for why this isn't agentmemory).
6. **Goals & plans**: pages + tables + agent tools.
7. **Midnight job** + push notifications.

## Verification

- `docker compose up` → 2 services healthy.
- Open Mini App from bot button (via tunnel) → Dashboard loads, auth OK.
- Link real Garmin creds → sync → rows in `daily_metrics`/`activities` (`docker compose exec postgres psql`), charts render.
- Chat "how did I sleep this week?" → visible token streaming, agent used `get_daily_metrics`, real numbers.
- DM the bot the same question → draft-streamed reply.
- Add goal "sub-50 10k by October" → row in `goals` + memory entry; fresh session recalls it.
- Manual midnight-job trigger (`python -m app.scheduler --run-now`) → progress push arrives.
- Real Garmin backfill (192 activities, 30 days metrics) and real coach chat both verified against the linked account.
- Garmin workout sync (`/api/plan/sync-garmin`) verified end-to-end against the real account with a disposable test workout (uploaded, scheduled, confirmed field shapes, then deleted/unscheduled — nothing left behind).
- Found and fixed: regenerating a plan via chat marks the old `TrainingPlan` "superseded" but its already-scheduled Garmin workouts were never touched, so re-syncing just piled new calendar entries on top of the old ones (real duplicates on the user's watch calendar). `sync_plan_to_garmin` now cleans up future-dated superseded-plan workouts (unschedule + delete on Garmin, clear local tracking columns) before pushing the active plan. Past-dated ones are left alone as historical record. Verified the cleanup query against real data (correctly targeted all 31 orphaned rows; user had already cleared them manually, confirmed query now returns 0).
- Per global instruction: **no commits until user tests and verifies**.

### Production deployment
- `deploy/` has a single install script (`install.sh`) for a DigitalOcean droplet the user creates manually — SSH in, run the script, done. Sized for the smallest tier (2GB RAM, 1-2 vCPU, 25GB disk): installs Docker, adds a 2GB swapfile (small droplets can OOM building the webapp/Python image otherwise), opens ufw for SSH/80/443 only, clones the repo, and starts the stack.
- `docker-compose.prod.yml` overlays the base compose file to add Caddy as a reverse proxy with automatic Let's Encrypt HTTPS (needs a real domain — no domain, no valid Mini App URL). Base `docker-compose.yml` now binds the backend's port to `127.0.0.1` only, so it's never reachable except through Caddy, in dev or prod.
- Full walkthrough in `deploy/README.md`. Not yet run against a real droplet — script is syntax-checked and the compose merge verified (`docker compose config`), but the DO-specific steps (apt, ufw, real DNS) haven't been exercised live.

## Risks / notes

- Unofficial Garmin API can break on Garmin-side changes; `garminconnect` actively maintained — expect occasional pin bumps.
- `sendMessageDraft` is recent; if aiogram hasn't wrapped it, use raw API call (trivial).
- Mini App requires public HTTPS even in dev — tunnel adds a setup step.
- Memory uses plain Postgres full-text search (`plainto_tsquery`), not semantic/vector search — an exact-ish keyword match, weaker recall than embeddings for paraphrased queries. Recent memories are also injected into every system prompt unconditionally, so the common case doesn't depend on search hitting. If recall quality becomes a real problem, `memory/client.py` is the seam to swap in pgvector + an embeddings API.
- LLM provider undecided — everything behind `LLM_BASE_URL`/`LLM_MODEL`; switching providers = env change.
- Single-user first; schema multi-user from day one (keyed by telegram_id).
