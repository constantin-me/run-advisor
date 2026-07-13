# Garmin Running Coach

Telegram Mini App that connects to Garmin Connect, learns your training/health data, and coaches your running via chat, goals, and generated training plans. See [MASTER.md](./MASTER.md) for the full technical plan.

There are two ways to run this locally:

- **[Local setup without Telegram](#local-setup-without-telegram)** — open the app straight in your web browser. You never need to open Telegram or message the bot. Best if you just want to try it out or develop on it. Written for people who aren't developers — every step is spelled out.
- **[Full Telegram Mini App setup](#full-telegram-mini-app-setup)** — the real experience, opened from inside Telegram. A bit more setup (needs a public HTTPS tunnel).

> **What's actually optional:** *Using* Telegram (opening the bot, messaging it, opening the app from inside Telegram) is entirely optional — that's the whole point of the local setup below. A Telegram bot **token** (a text credential from @BotFather) is not optional in either path: the backend reads it on startup regardless of which setup you follow. In the local-without-Telegram path you create one purely to get that token value; you never touch the bot itself afterward.

---

## Local setup without Telegram

This gets the whole app running on your own computer, opened in a normal browser tab like any website. Total time: about 15 minutes, most of it waiting for downloads.

### What you'll need

1. **A computer** running macOS, Windows, or Linux.
2. **Docker Desktop** — this is the only "real" software install required. It runs the app in an isolated container so you don't need to install Python, Node.js, or a database yourself.
   - Download it from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) and install it like any other app.
   - Open Docker Desktop once after installing and leave it running in the background — you'll see a whale icon in your menu bar / system tray when it's ready.
3. **A terminal** — a text-based window for typing commands.
   - Mac: open the "Terminal" app (search for it with Spotlight, ⌘+Space).
   - Windows: open "PowerShell" (search for it in the Start menu).
4. **An OpenAI API key** — this is what powers the AI coach's replies. Costs a small amount per use (a few cents for casual testing).
   - Go to [platform.openai.com/api-keys](https://platform.openai.com/api-keys), sign up/log in, and create a new key. It looks like `sk-...`. Copy it somewhere safe — you'll paste it in a moment.
5. **A Telegram bot token** — required for the app to start (see the callout above), free, takes under 2 minutes to get, and you will **not** need to open the resulting bot, message it, or install Telegram anywhere.
   - Open Telegram (app or [web.telegram.org](https://web.telegram.org)) and search for the user **@BotFather**.
   - Send it the message `/newbot`, then follow its prompts (pick any name, and a username ending in `bot`, e.g. `mycoach_test_bot`).
   - BotFather will reply with a token that looks like `123456789:AAExampleTokenTextHere`. Copy it.

### Step-by-step

**1. Download the project.**

If you have Git installed, open your terminal and run:

```bash
git clone https://github.com/constantin-melniciuc/run-advisor.git
cd run-advisor
```

If you don't have Git, go to [github.com/constantin-melniciuc/run-advisor](https://github.com/constantin-melniciuc/run-advisor), click the green "Code" button → "Download ZIP", unzip it, then in your terminal navigate into the unzipped folder, e.g.:

```bash
cd Downloads/run-advisor-main
```

**2. Create your configuration file.**

Every setting the app needs (API keys, passwords) lives in a file called `.env`, which you create by copying the provided template:

```bash
cp .env.example .env
```

(Windows PowerShell: use `copy .env.example .env` instead.)

**3. Edit `.env` and fill in your values.**

Open the `.env` file in any text editor (Notepad, TextEdit, VS Code — right-click the file → "Open with"). Change these lines:

```env
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
```

→ paste the token BotFather gave you. **Required to start the app** — but nothing more; you won't interact with this bot again in this setup path.

```env
WEBAPP_URL=https://example.trycloudflare.com
```

→ change to `WEBAPP_URL=http://localhost:8010`. Not optional to leave blank (the app needs some value here to start), but not used for anything real in this local-without-Telegram setup.

```env
LLM_API_KEY=sk-...
```

→ paste your OpenAI API key. Required — this is what actually powers the coach.

```env
JWT_SECRET=change-me-long-random
```

→ replace with any long, random string of your own making — e.g. mash your keyboard for 40 characters. Required, but any value works; it's just used internally to keep login sessions secure.

```env
DEV_MODE=false
```

→ change to `DEV_MODE=true`. **This is the setting that lets you skip Telegram entirely** — with it on, opening the app in a normal browser logs you in automatically as a local test user, and the Telegram bot never needs to be opened or used.

Leave everything else in `.env` as-is. Save the file.

**4. Start the app.**

Back in your terminal, run:

```bash
docker compose up --build -d
```

The first run downloads and builds everything, which can take a few minutes — that's normal. (`-d` runs it in the background so your terminal stays usable.) If you ever want to watch what's happening, run `docker compose logs -f backend`; press Ctrl+C to stop watching (this doesn't stop the app).

**5. Set up the database.**

Once step 4 finishes, run:

```bash
docker compose exec backend alembic upgrade head
```

This creates the database tables the app needs. You'll only need to re-run this in the future if you pull code updates that add new migrations.

**6. Open the app.**

Go to **[http://localhost:8010](http://localhost:8010)** in your web browser. You should land straight on the Dashboard — no login screen, thanks to `DEV_MODE=true`.

**7. Link your Garmin account.**

On the Dashboard, enter your Garmin Connect email and password and click **Link Garmin**. If Garmin asks for a verification code (MFA), enter it when prompted. Once linked, click **Backfill history** to pull in your past activities, or **Sync now** for just the last two days.

**8. Explore.**

Use the tabs at the bottom: **Today** (dashboard/metrics), **Chat** (talk to your coach), **Goals**, **Plan**.

### Stopping / restarting

- Stop everything: `docker compose down` (your data is preserved).
- Start it again later: `docker compose up -d` (no `--build` needed unless the code changed).

### Troubleshooting

- **"Port already in use" / "address already in use"** — something else on your computer is using port 8010 or 5432. Close it, or edit the port numbers on the left side of the `ports:` lines in `docker-compose.yml` (e.g. `8010:8000` → `8020:8000`), then reopen the app at the new port.
- **Docker Desktop isn't running** — you'll see connection errors. Open the Docker Desktop app and wait for the whale icon to show it's ready, then retry.
- **The page is blank or won't load** — give it another 10–20 seconds after step 4 finishes (the app is still starting up), then refresh.
- **Garmin login fails** — double-check your Garmin email/password work at [connect.garmin.com](https://connect.garmin.com) directly first.
- **Migration command fails with a connection error** — the database might still be starting up; wait 10 seconds and re-run the command from step 5.

---

## Full Telegram Mini App setup

The real deployment target: the app opens as a Mini App inside Telegram itself, reached via your bot's **Open Coach** button. Here, actually using Telegram is the point, not optional.

1. `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`, `JWT_SECRET`. Leave `DEV_MODE=false`.
2. Telegram Mini Apps require a public HTTPS URL, even in development. Expose your local server with a tunnel, e.g.:

   ```bash
   cloudflared tunnel --url http://localhost:8010
   ```

   Put the resulting URL into `WEBAPP_URL` in `.env`, and set it as your bot's Mini App URL via [@BotFather](https://t.me/BotFather) (`/setmenubutton` or `/newapp`).
3. `docker compose up --build -d`
4. Run migrations: `docker compose exec backend alembic upgrade head`
5. Open your bot in Telegram, send `/start`, tap **Open Coach**.

## Layout

- `backend/` — FastAPI + aiogram, single Python process (REST/SSE API, Telegram bot, scheduler).
- `webapp/` — Vite + React Mini App, built into `backend/static` at image build time.
- `docker-compose.yml` — backend, postgres.
- `deploy/` — DigitalOcean droplet provisioning script + Caddy config for a real HTTPS deployment. See `deploy/README.md`.
