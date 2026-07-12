# HRV / recovery nudges — design

## Overview

Proactively flag when the user's recovery signals (training readiness, HRV, resting HR, sleep) suggest an easy day or rest, instead of only surfacing this if the user happens to ask. Detection is **deterministic/rule-based**, not left to LLM discretion — this guarantees the nudge actually fires consistently rather than depending on the model noticing a dip buried in a data dump.

## Why not just let the LLM decide from raw metrics?

The coach already receives 7 days of metrics in its system prompt (`_format_metrics` in `agent.py`) and could theoretically notice a bad HRV day on its own. In practice this is unreliable — the model may or may not weight it heavily depending on what else is being discussed. A dedicated rule engine that runs on every sync and explicitly labels the day green/yellow/red removes that variance, and gives the nightly push job a hard trigger to lead with instead of relying on the free-text instruction alone.

## New module: `backend/app/coach/recovery.py`

```python
@dataclass
class RecoveryStatus:
    level: Literal["green", "yellow", "red"]
    reasons: list[str]
    metric_date: date          # which day's data this status describes
    training_readiness: int | None
    hrv_delta_pct: float | None
    rhr_delta: int | None

async def compute_recovery_status(db: AsyncSession, user: User) -> RecoveryStatus | None:
    ...
```

Returns `None` when there isn't enough usable data rather than a misleading status.

### Which day gets evaluated (data freshness)

`DailyMetric` rows only exist after a sync, and chat does **not** trigger a sync — the freshest row during a normal day is usually yesterday's (written by the midnight job) unless the user ran `/sync`. So:

- Evaluate the **most recent row within the last 2 days** (today's if present, else yesterday's).
- If neither exists, return `None` — no flag, no guessing.
- `metric_date` is carried through to every consumer so wording stays honest: "Recovery flag (based on yesterday's data): …". Never present stale data as "today".

### Signal priority

1. **Primary: `DailyMetric.training_readiness`.** Garmin's sync already lands this field (`sync.py` pulls it from `get_training_readiness`) — it's a pre-computed 0-100 composite Garmin itself derives from HRV, sleep, stress, and training load, so it's the most reliable single signal already available with zero extra work.
   - Proposed cutoffs: `<40` → red, `40-59` → yellow, `>=60` → green.
   - **Needs validation against real production data before finalizing** — there are real synced rows in the prod DB already; check the actual value distribution and cross-reference the qualitative labels ("Low"/"Moderate"/"High") Garmin's own app shows for the same days.

2. **Fallback: rolling-baseline HRV/RHR deviation**, used only when `training_readiness` is missing for the day (Garmin doesn't always return it, per the existing `_dig`-based defensive parsing in `sync.py`).
   - Note on semantics: the stored `hrv` is Garmin's `lastNightAvg` — an overnight average, so "day X's HRV" describes the night leading into day X. That's exactly the right signal for "how recovered am I this morning".
   - Baseline = mean of the prior 7 days for `hrv` and `resting_hr`, **excluding the evaluated day and skipping `None` days**; require at least 3 valid days, else insufficient data (`None` for this signal, don't guess).
   - `hrv_delta_pct = (day_hrv - baseline_hrv) / baseline_hrv * 100`
   - Red if `hrv_delta_pct <= -20` or `rhr_delta >= +7`
   - Yellow if `hrv_delta_pct <= -10` or `rhr_delta >= +4`

3. **Secondary factor** (added to `reasons`, can escalate yellow→red when stacked with a primary flag, but doesn't independently trigger): low sleep score (e.g. `<50`).

**Deliberately excluded: body battery.** The synced value is `bodyBatteryMostRecentValue` — i.e. whatever it was *at sync time*. The midnight job syncs at end of day, when body battery is naturally depleted for everyone, so "low body battery" from stored data would flag almost every day as bad. A waking/peak body battery value isn't in what we currently store; revisit only if the sync starts extracting it from the raw payload (see Open questions).

All thresholds as named constants at the top of the file for easy tuning later — this is a first-pass calibration, not a claim of clinical accuracy.

### Data access

No new tables or migration — pure computation over existing `DailyMetric` rows, fetched the same way `get_daily_metrics` already does in `coach/tools.py` (reuse that function with `days=14` for enough history to compute the baseline, rather than writing a parallel query).

## Integration points

1. **`_build_system_prompt()` (`backend/app/coach/agent.py`)** — call `compute_recovery_status`; when not green, inject a short "Recovery flag (yesterday's data): yellow — HRV down 14% vs 7-day baseline" section, same pattern as the existing metrics/goals/plan blocks. Makes every chat turn recovery-aware without a tool round-trip. Wrap in try/except (log + omit) so a bug here can never break chat.

2. **New coach tool `get_recovery_status`** (`backend/app/coach/tools.py`, same `TOOL_SCHEMAS`/`execute_tool` pattern as the rest) — lets the model re-check status explicitly mid-conversation (e.g. after the user mentions how they're feeling), rather than relying only on the value baked into the system prompt at conversation start.

3. **Midnight job (`run_user_progress` in `backend/app/scheduler.py`)** — after `sync_day`, compute status. When yellow/red, prepend an explicit directive to the `generate_progress_summary` instruction, e.g.:
   > "IMPORTANT: recovery signals for {metric_date} are flagged ({level}) — reasons: {reasons}. Lead the message with this and give one concrete recommendation (easy day, rest, extra sleep) before anything else."

   This is the deterministic trigger — the push message reliably surfaces the flag instead of hoping the LLM decides to mention it. (The job syncs *yesterday's* data, so `metric_date` will be yesterday — the directive's wording, not a hardcoded "today", keeps the push accurate.)

4. **`backend/app/coach/prompts.py`** — short addition to `SYSTEM_PROMPT` telling the coach to weigh recovery status when giving training advice (e.g. don't recommend a hard tempo run on a red day without at least flagging the tradeoff) and to proactively mention it when flagged, not just when asked.

5. **Bot command `/recovery`** (`backend/app/telegram/bot.py`) — on-demand check, replying via `_send_html`. Wiring specifics that bit us before: add it to the `BOT_COMMANDS` list (feeds `set_my_commands` — otherwise no "/" autocomplete), register the handler **before** the catch-all `@dp.message(F.text)` handler, and reply with the standard "Garmin isn't linked yet…" message when `garmin_linked` is false (same guard as `/sync`). When status is `None` (no recent data), say so and suggest `/sync` rather than replying with nothing.

## Verification

- Unit-test `compute_recovery_status` against fixture `DailyMetric` rows: green day, readiness-red day, readiness-missing-but-HRV-crashed day, sleep-score escalation, brand-new user (`None`), data older than 2 days (`None`), baseline with `None` gaps.
- Backend import check (`python -c "from app.main import app"`).
- Threshold sanity check against real data: query the prod DB's existing `training_readiness` / `hrv` history and confirm the proposed cutoffs would have flagged days that felt genuinely rough (the user can eyeball the resulting labels per day).
- Live after deploy: `/recovery` (both linked and hypothetical unlinked path), a chat question like "should I do my tempo run today?" on a flagged day, and the midnight push via the existing `python -m app.scheduler --run-now` manual trigger to confirm a yellow/red day actually leads the message.

## Open questions

- Exact `training_readiness` cutoffs — validate against the real prod distribution before locking (see Verification).
- Alert fatigue: should N consecutive flagged days suppress repeat nudges (e.g. only lead with it on the first day of a streak, then fold it into a lighter mention), or is a fresh flag every day actually useful? Leaning toward "mention every day it's flagged, but only escalate to leading the message the first day of a new streak" — worth deciding once there's real usage to react to rather than guessing.
- Body battery: worth re-adding if the sync starts extracting a waking/peak value from the raw stats payload (`raw` JSON is already stored per day, so a backfill would even be possible) — but only with a value that isn't sync-time-dependent.
