from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyMetric, User

# First-pass calibration, not a claim of clinical accuracy — tune against
# real usage. See Recovery.md open questions.
READINESS_RED_THRESHOLD = 40  # score below this -> red
READINESS_YELLOW_THRESHOLD = 60  # score below this (and not red) -> yellow
HRV_RED_DROP_PCT = -20.0
HRV_YELLOW_DROP_PCT = -10.0
RHR_RED_DELTA = 7
RHR_YELLOW_DELTA = 4
LOW_SLEEP_SCORE = 50

MIN_BASELINE_DAYS = 3
BASELINE_WINDOW_DAYS = 7
FRESHNESS_WINDOW_DAYS = 2

Level = Literal["green", "yellow", "red"]


@dataclass
class RecoveryStatus:
    level: Level
    reasons: list[str]
    metric_date: date  # which day's data this status describes — may be yesterday's
    training_readiness: int | None
    hrv_delta_pct: float | None
    rhr_delta: int | None


def _escalate(level: Level, candidate: Level) -> Level:
    order: dict[Level, int] = {"green": 0, "yellow": 1, "red": 2}
    return candidate if order[candidate] > order[level] else level


async def compute_recovery_status(
    db: AsyncSession, user: User
) -> RecoveryStatus | None:
    """Deterministic recovery flag — never left to the LLM's judgment, so it
    fires consistently rather than depending on the model noticing a dip
    buried in a data dump. Returns None when there isn't enough usable data
    (brand-new user, or nothing synced in the last two days) rather than a
    misleading status."""
    since = date.today() - timedelta(days=BASELINE_WINDOW_DAYS + FRESHNESS_WINDOW_DAYS)
    result = await db.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user.id, DailyMetric.metric_date >= since)
        .order_by(DailyMetric.metric_date.desc())
    )
    rows = list(result.scalars().all())
    if not rows:
        return None

    freshness_cutoff = date.today() - timedelta(days=FRESHNESS_WINDOW_DAYS)
    today_row = next((r for r in rows if r.metric_date >= freshness_cutoff), None)
    if today_row is None:
        return None

    reasons: list[str] = []
    level: Level = "green"
    hrv_delta_pct: float | None = None
    rhr_delta: int | None = None

    if today_row.training_readiness is not None:
        tr = today_row.training_readiness
        if tr < READINESS_RED_THRESHOLD:
            level = _escalate(level, "red")
            reasons.append(f"training readiness {tr} (low)")
        elif tr < READINESS_YELLOW_THRESHOLD:
            level = _escalate(level, "yellow")
            reasons.append(f"training readiness {tr} (moderate)")
    else:
        baseline_rows = [
            r
            for r in rows
            if r.metric_date < today_row.metric_date
            and r.metric_date
            >= today_row.metric_date - timedelta(days=BASELINE_WINDOW_DAYS)
        ]
        hrv_baseline = [float(r.hrv) for r in baseline_rows if r.hrv is not None]
        rhr_baseline = [r.resting_hr for r in baseline_rows if r.resting_hr is not None]

        if today_row.hrv is not None and len(hrv_baseline) >= MIN_BASELINE_DAYS:
            baseline_avg = sum(hrv_baseline) / len(hrv_baseline)
            hrv_delta_pct = (float(today_row.hrv) - baseline_avg) / baseline_avg * 100
            if hrv_delta_pct <= HRV_RED_DROP_PCT:
                level = _escalate(level, "red")
                reasons.append(
                    f"HRV down {abs(hrv_delta_pct):.0f}% vs your {len(hrv_baseline)}-day baseline"
                )
            elif hrv_delta_pct <= HRV_YELLOW_DROP_PCT:
                level = _escalate(level, "yellow")
                reasons.append(
                    f"HRV down {abs(hrv_delta_pct):.0f}% vs your {len(hrv_baseline)}-day baseline"
                )

        if today_row.resting_hr is not None and len(rhr_baseline) >= MIN_BASELINE_DAYS:
            baseline_avg = sum(rhr_baseline) / len(rhr_baseline)
            rhr_delta = round(today_row.resting_hr - baseline_avg)
            if rhr_delta >= RHR_RED_DELTA:
                level = _escalate(level, "red")
                reasons.append(
                    f"resting HR up {rhr_delta} bpm vs your {len(rhr_baseline)}-day baseline"
                )
            elif rhr_delta >= RHR_YELLOW_DELTA:
                level = _escalate(level, "yellow")
                reasons.append(
                    f"resting HR up {rhr_delta} bpm vs your {len(rhr_baseline)}-day baseline"
                )

    # Secondary factor: per design, never triggers a flag on its own — only
    # escalates yellow->red when stacked with an existing primary signal.
    if (
        today_row.sleep_score is not None
        and today_row.sleep_score < LOW_SLEEP_SCORE
        and level != "green"
    ):
        reasons.append(f"sleep score {today_row.sleep_score} (low)")
        level = _escalate(level, "red")

    return RecoveryStatus(
        level=level,
        reasons=reasons,
        metric_date=today_row.metric_date,
        training_readiness=today_row.training_readiness,
        hrv_delta_pct=hrv_delta_pct,
        rhr_delta=rhr_delta,
    )
