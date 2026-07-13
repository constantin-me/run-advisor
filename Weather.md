# Weather awareness — design

## Overview

Give the coach forecast data so it can factor heat, humidity, wind, and rain into training advice and proactive nudges, instead of reasoning about workouts in a vacuum.

Location is **auto-detected from the user's own outdoor Garmin activities** — no onboarding question, no manual step for the common case. `/location <city>` exists only as a manual override/fallback. Users whose activities are all indoor (treadmill, pool, gym) never get asked for a location and never see a weather section — weather genuinely doesn't matter to them, so the feature stays invisible rather than nagging.

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

## Location auto-detection from outdoor activities

**Finding**: Garmin's activity data already contains everything needed to set location automatically, but only through the right endpoint. Confirmed against real synced data in the local/prod DB (`Activity.raw`):

- `sync_day()` — the routine path used by the midnight job and `/sync` — calls `client.get_activities_fordate()`, which returns a reduced ~33-field payload with **no GPS data at all**, regardless of whether the activity was outdoors.
- The one-off `sync_recent_activities()` backfill calls `client.get_activities()` (the bulk/offset endpoint), which returns a richer ~75-85-field payload that, for outdoor GPS activities, includes `startLatitude`, `startLongitude`, `endLatitude`, `endLongitude`, and — most usefully — **`locationName`**, a human-readable place Garmin has already resolved (real values seen: `"Munich"`, `"Capdepera"`). Indoor activities (treadmill, pool swimming) correctly have none of these fields, confirming it's GPS presence driving it, not just a schema difference by activity type.

**Design**: rather than maintaining an allowlist of "indoor" `activity_type` values (fragile — Garmin can add types, and we'd have to keep it in sync), detect outdoor-vs-indoor empirically and self-correctingly:

1. In `sync_day()`, after the normal activity upsert, **only while `user.latitude is None`** (i.e. this entire step is skipped forever once a location exists — near-zero ongoing cost): take the most recent activity synced that day, if any, and call `client.get_activity(activity_id)` (single-activity summary endpoint — one extra API call, only for brand-new activities on days a location is still unset).
2. If the response includes `startLatitude`/`startLongitude`, persist `latitude`, `longitude`, and `location_name` (straight from Garmin's own `locationName` — no need to call our own `geocode()` for this path) directly onto `User`.
3. Resolve `timezone` from the coordinates using the **`timezonefinder`** package (pure offline lat/lon → IANA timezone lookup, no network call, no new external dependency on Open-Meteo for this path) — new dependency to add to `backend/pyproject.toml`.
4. If the activity has no GPS fields (indoor), do nothing — no error, no flag, just try again next sync when there's a new activity. For a user who only ever does treadmill/pool sessions, this silently never fires, which is exactly the desired behavior: no location, no weather section, no unwanted prompting.

This means the coach's guidance in `prompts.py` to proactively *ask* for a location (see System prompt integration) becomes the fallback path — reached only for users who haven't yet synced an outdoor activity (e.g. brand new, or an upcoming trip to a city they haven't run in yet) — not the primary way location gets set.

`set_location` / `/location <city>` remain as an explicit **override**: useful for correcting a wrong auto-detected city, setting a location ahead of travel, or for the rare case someone wants forecasts without having logged an outdoor run yet.

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

- `set_location(city: str)` — manual override. Geocodes and persists `latitude`/`longitude`/`location_name`/`timezone` on `User`. Returns the resolved location name so the model can confirm it back to the user ("Got it, using Bucharest, Romania"). Only expected to be called when the user explicitly names a city, or as the fallback ask described below — auto-detection (see above) is the primary path.
- `get_weather_forecast(days: int = 3)` — returns the forecast (daily + windows) if location is set (whether auto-detected or manually set — the tool doesn't care which), otherwise `{"error": "no_location", "message": "ask the user for their city, then call set_location"}` so the model knows what to do next rather than failing silently.

Both follow the existing `TOOL_SCHEMAS` + `execute_tool()` dispatch pattern already used by `get_daily_metrics`, `save_goal`, etc.

## System prompt integration

`_build_system_prompt()` in `backend/app/coach/agent.py` already eagerly injects metrics/goals/plan into every conversation (not gated behind a tool call). Add a same-pattern block:

```
Weather (next 2 days, local time): 12 Jul: 22-31°C (feels like up to 34°C), 10% rain, wind 14km/h (clear); morning 19°C / midday 29°C / evening 27°C. 13 Jul: ...
```

— only when `user.latitude` is set (typically auto-detected by now, see above); otherwise omit the section entirely (no "no weather data" noise, matching how the metrics section behaves when nothing's synced yet). Also omitted silently when the fetch fails (see Failure handling).

Also add a short paragraph to `backend/app/coach/prompts.py`'s `SYSTEM_PROMPT` telling the coach to factor forecast conditions (especially feels-like temperature and the time-of-day windows) into pace/hydration/scheduling advice for outdoor workouts. This is now framed as a **fallback**, not the primary way location gets set: only ask for a city when weather would clearly be useful (e.g. discussing an upcoming outdoor workout) *and* no location is set yet — which by this point mostly means brand-new users or people who haven't logged an outdoor activity, not the general case. Never proactively ask a user whose synced activities are all indoor; there's nothing in their data suggesting weather is relevant to them, so the coach simply has no reason to bring it up.

## Midnight job integration

`run_user_progress()` in `backend/app/scheduler.py` already builds a free-text instruction for `generate_progress_summary()`. When location is set, include tomorrow's forecast — **"tomorrow" computed in the user's stored timezone**, since the job fires at UTC midnight — in that instruction so the nightly push can proactively flag conditions before a scheduled workout (e.g. "Tomorrow's tempo run: 29°C by 11am — go before 9 or ease off goal pace by ~15s/km").

## Bot commands (`backend/app/telegram/bot.py`)

Same pattern as the existing `/sync`/`/lastworkout` commands, with the wiring details that bit us before made explicit:
- `/weather` — on-demand forecast reply via `_send_html`. Most users will never need to run `/location` first — this just works once they've synced an outdoor activity.
- `/location <city>` — manual override; parses the text after the command, calls the same geocode logic as the `set_location` tool. **With no argument**, replies with the currently-set location (auto-detected or manual — same field either way) or a usage hint if none.
- Both handlers registered **before** the catch-all `@dp.message(F.text)` handler, like the existing commands.
- Both added to the `BOT_COMMANDS` list so `set_my_commands` publishes them — otherwise they won't show in Telegram's "/" autocomplete menu (exactly the issue hit with the first command batch).

## Explicitly out of scope for MVP

- No webapp settings page for location — chat/command-driven only. Worth revisiting once there's a natural "profile/settings" surface in the webapp.
- No historical weather correlation (e.g. "your paces are 20s/km slower above 25°C") — plausible future extension once there's enough activity history, but adds real complexity (matching activity timestamps to historical weather) for a first pass.
- No per-user-local midnight job scheduling (the stored timezone enables it later).

## Verification

- `curl` both Open-Meteo endpoints once by hand **before coding against them** to confirm actual response shapes (field names above are from docs; this session's rule is trust-but-verify external APIs).
- Unit-test the WMO-code → text mapping and the hourly → morning/midday/evening summarizer against fixture JSON captured from the real API.
- Confirm against the real DB (already done during research for this doc) that `startLatitude`/`startLongitude`/`locationName` are present in `get_activity()`/`get_activities()` responses for outdoor activities and absent for indoor ones — re-verify against a live `get_activity(activity_id)` call (the single-activity endpoint `sync_day` will use) specifically, since only the bulk `get_activities()` shape was directly inspected so far.
- Unit-test the auto-detection step: outdoor activity → location persisted + `timezonefinder` resolves a sane IANA timezone for the coordinates; indoor activity → no change, no error; `user.latitude` already set → the enrichment call is skipped entirely (assert no extra `get_activity` call happens).
- Backend import check (`python -c "from app.main import app"`) and `alembic upgrade head` against the local dev DB.
- Live (after deploy — pipeline already runs `alembic upgrade head` automatically, see `.github/workflows/deploy.yml:23`): sync a real outdoor run and confirm location auto-populates without running `/location`; separately confirm `/location Bucharest` still works as a manual override; then a chat question like "should I run tomorrow morning?" and confirm the answer cites real forecast numbers with correct local-day labels.
- Failure path: point the client at an unreachable host locally and confirm chat still answers (weather section silently omitted).

## Open questions

- What feels-like/humidity thresholds actually warrant a warning vs. just informational text? Needs tuning against real usage rather than guessing up front.
- Should an ambiguous manually-entered city name always be confirmed with the user, or is picking the top geocoding match and stating it back sufficient? Leaning toward the latter (state it back, let them correct) to avoid an extra round-trip for the common case. (Doesn't apply to auto-detection — Garmin's `locationName` is used as-is, no ambiguity to resolve.)
- Should auto-detecting a location trigger a one-time confirmation push ("Noticed you ran in Munich — using this for weather from now on. Change anytime with /location.") or stay fully silent until the user asks for weather/gets a nudge that mentions it naturally? Leaning toward a one-time notice — silent auto-changes to stored data feel surprising if the user later wonders why the coach suddenly knows about weather.
- Cost optimization: skip the `get_activity()` enrichment call outright for activity types that are unambiguously indoor (`lap_swimming`, `pool_swimming`, `strength_training`, `treadmill_running`, `indoor_cycling`, `yoga`) using the type already captured by the cheaper `get_activities_fordate` payload, rather than making the call and discovering no GPS fields. Optional — the self-correcting no-op path already makes this a pure cost optimization, not a correctness fix, so it can be added later if the extra API calls turn out to matter.
- Window boundaries (6–9/11–14/17–20) are a first guess — worth asking the user when they typically run and biasing the summary to that window later (could even be a stored memory the coach already supports).
