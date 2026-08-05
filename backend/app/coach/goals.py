"""Deterministic-ish goal achievement checks against recent activities.

Free-text goals stay free-text; we only auto-detect clear distance / pace
patterns so chat and the daily push can congratulate without waiting for the
LLM to notice."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Activity, Goal, User

LOOKBACK_DAYS = 14
# Accept a run a bit short of the labeled distance (GPS under-report, course).
DISTANCE_TOLERANCE = 0.97

_KM_RE = re.compile(
    r"(?P<n>\d+(?:\.\d+)?)\s*(?:k(?:m)?|kilometers?)\b",
    re.IGNORECASE,
)
_MI_RE = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*mi(?:les?)?\b", re.IGNORECASE)
# "sub-50 10k" / "sub 50 10k" - minutes for the whole distance.
_SUB_TOTAL_MIN_RE = re.compile(
    r"sub[-\s]?(?P<min>\d{1,3})(?:\s*(?:min(?:utes?)?)?)?",
    re.IGNORECASE,
)
# "sub-5:00/km" or "sub 5:00" with pace context.
_SUB_PACE_RE = re.compile(
    r"sub[-\s]?(?P<m>\d{1,2}):(?P<s>\d{2})\s*(?:/?\s*km)?",
    re.IGNORECASE,
)


def _parse_target_distance_m(text: str) -> float | None:
    km = _KM_RE.search(text)
    if km:
        return float(km.group("n")) * 1000.0
    mi = _MI_RE.search(text)
    if mi:
        return float(mi.group("n")) * 1609.344
    return None


def _parse_target_duration_s(text: str, distance_m: float | None) -> float | None:
    """Total-time target from 'sub-N' when N looks like minutes for the race
    (typical for 5k/10k/half goals), not a per-km pace."""
    pace = _SUB_PACE_RE.search(text)
    if pace and distance_m:
        s_per_km = int(pace.group("m")) * 60 + int(pace.group("s"))
        return s_per_km * (distance_m / 1000.0)

    total = _SUB_TOTAL_MIN_RE.search(text)
    if total and distance_m:
        minutes = int(total.group("min"))
        # Heuristic: single- or two-digit "sub-5" near a distance is more often
        # a total-time goal for short races (sub-20 5k) than a pace; three-digit
        # is always total minutes (sub-120 half).
        if minutes >= 10 or distance_m >= 4000:
            return float(minutes * 60)
    return None


def _parse_target_pace_s_per_km(text: str) -> float | None:
    pace = _SUB_PACE_RE.search(text)
    if pace:
        return float(int(pace.group("m")) * 60 + int(pace.group("s")))
    return None


def _activity_matches_distance(activity: Activity, target_m: float) -> bool:
    if activity.distance_m is None:
        return False
    return float(activity.distance_m) >= target_m * DISTANCE_TOLERANCE


def _evaluate_one(goal: Goal, activities: list[Activity]) -> dict | None:
    text = goal.text
    target_m = _parse_target_distance_m(text)
    if target_m is None:
        return None

    matching = [a for a in activities if _activity_matches_distance(a, target_m)]
    if not matching:
        return None

    target_duration_s = _parse_target_duration_s(text, target_m)
    target_pace = _parse_target_pace_s_per_km(text)

    if target_duration_s is not None:
        for a in matching:
            if a.duration_s is not None and float(a.duration_s) <= target_duration_s:
                dist_km = float(a.distance_m) / 1000.0
                mins = float(a.duration_s) / 60.0
                return {
                    "goal_id": goal.id,
                    "text": goal.text,
                    "evidence": (
                        f"ran {dist_km:.2f} km in {mins:.1f} min "
                        f"(target under {target_duration_s / 60:.0f} min at ~{target_m / 1000:.1f} km)"
                    ),
                }
        return None

    if target_pace is not None:
        for a in matching:
            if (
                a.avg_pace_s_per_km is not None
                and float(a.avg_pace_s_per_km) <= target_pace
            ):
                pace = float(a.avg_pace_s_per_km)
                m, s = divmod(int(pace), 60)
                return {
                    "goal_id": goal.id,
                    "text": goal.text,
                    "evidence": (
                        f"ran {float(a.distance_m) / 1000:.2f} km at {m}:{s:02d}/km "
                        f"(target ≤ {int(target_pace) // 60}:{int(target_pace) % 60:02d}/km)"
                    ),
                }
        return None

    # Distance-only goal (e.g. "run a 10k").
    best = max(matching, key=lambda a: float(a.distance_m or 0))
    dist_km = float(best.distance_m) / 1000.0
    when = best.start_time.date().isoformat() if best.start_time else "recently"
    return {
        "goal_id": goal.id,
        "text": goal.text,
        "evidence": f"ran {dist_km:.2f} km on {when} (target ~{target_m / 1000:.1f} km)",
    }


async def evaluate_goal_achievements(
    db: AsyncSession, user: User, *, lookback_days: int = LOOKBACK_DAYS
) -> list[dict]:
    """Return active goals clearly met by recent activities.

    Each item: {goal_id, text, evidence}. Ambiguous goals are omitted for the
    LLM to judge."""
    goals_result = await db.execute(
        select(Goal).where(Goal.user_id == user.id, Goal.status == "active")
    )
    goals = list(goals_result.scalars().all())
    if not goals:
        return []

    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    act_result = await db.execute(
        select(Activity)
        .where(
            Activity.user_id == user.id,
            Activity.start_time.is_not(None),
            Activity.start_time >= since,
        )
        .order_by(Activity.start_time.desc())
    )
    activities = list(act_result.scalars().all())
    if not activities:
        return []

    hits: list[dict] = []
    for goal in goals:
        hit = _evaluate_one(goal, activities)
        if hit:
            hits.append(hit)
    return hits


def format_achievement_directive(hits: list[dict]) -> str:
    lines = [
        "IMPORTANT: the following active goals appear ACHIEVED based on recent "
        "activities (deterministic check — not your judgment):"
    ]
    for h in hits:
        lines.append(f"- goal_id={h['goal_id']} \"{h['text']}\": {h['evidence']}")
    lines.append(
        "Congratulate the user in 1–2 sentences, call complete_goal for each "
        "goal_id listed, then suggest 2–3 next goals (bump distance, improve "
        "pace, lower avg HR, or combine). Offer to save_goal one — do not save "
        "unless they pick."
    )
    return "\n".join(lines)
