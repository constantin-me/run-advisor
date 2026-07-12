# Weather awareness — design

## Overview

Give the coach forecast data so it can factor heat, humidity, wind, and rain into training advice and proactive nudges, instead of reasoning about workouts in a vacuum. Chat/command-driven for MVP — no webapp UI changes.

Two granularities matter to a runner:
- **Daily outlook** (next 3 days) — for planning which day to place a hard workout.
- **Time-of-day windows** (next 48h) — for deciding *when* to run: a 31°C-max day can still have a pleasant 18°C morning. Daily min/max alone can't answer "what will it be like at 7am?"

## Provider

**Open-Meteo** (`open-meteo.com`) for both geocoding and forecast:
- `https://geocoding-api.open-meteo.com/v1/search?name=<query>` — turns a city name into `{name, country, latitude, longitude, timezone}` (IANA timezone comes free with the result — persist it, see Data model).
- `https://api.open-meteo.com/v1/forecast` with:
  - `daily=temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,precipitation_probability_max,windspeed_10m_max,weather_code`
  - `hourly=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation_probability` (48h)
  - `timezone=<user's stored IANA timezone>` so day boundaries and hourly timestamps are local to the user, not UTC.

Notes:
- Open-Meteo's **daily tier has no humidity variable** — humidity comes from the hourly tier (`relative_humidity_2m`); the daily outlook uses `apparent_temperature_*` (feels-like, which already factors humidity + wind and is the more actionable signal for runners than raw humidity %).
- `weather_code` is the current param name (`weathercode` is legacy).
- No API key required, generous free tier — avoids adding a secret to `Settings`/`.env`/the deploy scripts for an MVP.

## Data model

Add to `User` (`backend/app/db/models.py`):
- `latitude: Mapped[float | None]` (Numeric — matches existing model conventions)
- `longitude: Mapped[float | None]` (Numeric)
- `location_name: Mapped[str | None]` (String) — display name from geocoding, e.g. "Bucharest, Romania"
- `timezone: Mapped[str | None]` (String) — IANA name from geocoding, e.g. "Europe/Bucharest"

One Alembic migration, all nullable — existing users default to "no location set" and every weather path degrades gracefully from there.

Storing the timezone matters beyond weather: `Settings.tz` is a single global (UTC in prod) and the midnight job fires at UTC midnight for everyone. Passing the user's timezone to the forecast API keeps "today"/"tomorrow" labels correct in the nightly push (computed at UTC midnight, which is already tomorrow in e.g. Bucharest). Making the midnight job itself run per-user-local is a natural follow-up but **out of scope here**.

## New module: `backend/app/weather/client.py`

- `geocode(query: str) -> dict | None` — calls the geocoding endpoint, returns the top match (`name`, `country`, `latitude`, `longitude`, `timezone`) or `None`. On ambiguous results, return the top match but let the caller (the `set_location` tool) mention the resolved name back to the user so they can correct it if wrong.
- `get_forecast(lat: float, lon: float, tz: str, days: int = 3) -> dict` — returns:
  - `daily`: per-day `{date, temp_min, temp_max, feels_like_min, feels_like_max, precipitation_probability, wind_speed_max, condition}` where `condition` is short text derived from the WMO `weather_code`;
  - `windows`: for the next 48h, per-day **morning (6–9) / midday (11–14) / evening (17–20)** summaries `{temp, feels_like, humidity, precipitation_probability}` averaged from the hourly series — this is what lets the coach say "run before 9am, it'll be 19°C instead of 30°C".
- **HTTP**: async `httpx` (already a dependency) with a ~5s timeout.
- **Caching**: a small in-process TTL cache (~30 min, keyed by rounded lat/lon to 2 decimal places) — without this, `_build_system_prompt` would hit the API on every single chat message, which is wasteful even against a free API.

## Failure handling

The weather API being down or slow must never degrade chat:
- `weather/client.py` raises/returns `None` on timeout or non-200; it never retries in-line (the ~5s timeout is the whole budget).
- `_build_system_prompt` wraps the forecast fetch in try/except: log and **omit the weather section** — same swallow-and-log philosophy as `sync.py`'s per-source fetches.
- Tools return an error dict (`{"error": "weather_unavailable"}`) the model can relay conversationally instead of crashing the tool loop.

## New coach tools (`backend/app/coach/tools.py`)

- `set_location(city: str)` — geocodes and persists `latitude`/`longitude`/`location_name`/`timezone` on `User`. Returns the resolved location name so the model can confirm it back to the user ("Got it, using Bucharest, Romania").
- `get_weather_forecast(days: int = 3)` — returns the forecast (daily + windows) if location is set, otherwise `{"error": "no_location", "message": "ask the user for their city, then call set_location"}` so the model knows what to do next rather than failing silently.

Both follow the existing `TOOL_SCHEMAS` + `execute_tool()` dispatch pattern already used by `get_daily_metrics`, `save_goal`, etc.

## System prompt integration

`_build_system_prompt()` in `backend/app/coach/agent.py` already eagerly injects metrics/goals/plan into every conversation (not gated behind a tool call). Add a same-pattern block:

```
Weather (next 2 days, local time): 12 Jul: 22-31°C (feels like up to 34°C), 10% rain, wind 14km/h (clear); morning 19°C / midday 29°C / evening 27°C. 13 Jul: ...
```

— only when `user.latitude` is set; otherwise omit the section entirely (no "no weather data" noise, matching how the metrics section behaves when nothing's synced yet). Also omitted silently when the fetch fails (see Failure handling).

Also add a short paragraph to `backend/app/coach/prompts.py`'s `SYSTEM_PROMPT` telling the coach to factor forecast conditions (especially feels-like temperature and the time-of-day windows) into pace/hydration/scheduling advice for outdoor workouts, and to ask for + save the user's location the first time weather would be useful.

## Midnight job integration

`run_user_progress()` in `backend/app/scheduler.py` already builds a free-text instruction for `generate_progress_summary()`. When location is set, include tomorrow's forecast — **"tomorrow" computed in the user's stored timezone**, since the job fires at UTC midnight — in that instruction so the nightly push can proactively flag conditions before a scheduled workout (e.g. "Tomorrow's tempo run: 29°C by 11am — go before 9 or ease off goal pace by ~15s/km").

## Bot commands (`backend/app/telegram/bot.py`)

Same pattern as the existing `/sync`/`/lastworkout` commands, with the wiring details that bit us before made explicit:
- `/weather` — on-demand forecast reply via `_send_html`.
- `/location <city>` — explicit set; parses the text after the command, calls the same geocode logic as the `set_location` tool. **With no argument**, replies with the currently-set location (or a usage hint if none) instead of failing silently.
- Both handlers registered **before** the catch-all `@dp.message(F.text)` handler, like the existing commands.
- Both added to the `BOT_COMMANDS` list so `set_my_commands` publishes them — otherwise they won't show in Telegram's "/" autocomplete menu (exactly the issue hit with the first command batch).

## Explicitly out of scope for MVP

- No webapp settings page for location — chat/command-driven only. Worth revisiting once there's a natural "profile/settings" surface in the webapp.
- No historical weather correlation (e.g. "your paces are 20s/km slower above 25°C") — plausible future extension once there's enough activity history, but adds real complexity (matching activity timestamps to historical weather) for a first pass.
- No per-user-local midnight job scheduling (the stored timezone enables it later).

## Verification

- `curl` both Open-Meteo endpoints once by hand **before coding against them** to confirm actual response shapes (field names above are from docs; this session's rule is trust-but-verify external APIs).
- Unit-test the WMO-code → text mapping and the hourly → morning/midday/evening summarizer against fixture JSON captured from the real API.
- Backend import check (`python -c "from app.main import app"`) and `alembic upgrade head` against the local dev DB.
- Live (after deploy — pipeline already runs `alembic upgrade head` automatically, see `.github/workflows/deploy.yml:23`): `/location Bucharest`, `/weather`, then a chat question like "should I run tomorrow morning?" and confirm the answer cites real forecast numbers with correct local-day labels.
- Failure path: point the client at an unreachable host locally and confirm chat still answers (weather section silently omitted).

## Open questions

- What feels-like/humidity thresholds actually warrant a warning vs. just informational text? Needs tuning against real usage rather than guessing up front.
- Should an ambiguous city name always be confirmed with the user, or is picking the top geocoding match and stating it back sufficient? Leaning toward the latter (state it back, let them correct) to avoid an extra round-trip for the common case.
- Window boundaries (6–9/11–14/17–20) are a first guess — worth asking the user when they typically run and biasing the summary to that window later (could even be a stored memory the coach already supports).
