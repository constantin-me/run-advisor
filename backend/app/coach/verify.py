"""Deterministic verification of what actually got saved.

The coach is an LLM: it can believe it wrote a six-week plan after writing two
weeks, or report a session as synced when the push failed. Nothing in the model
can be trusted to audit itself, so every save is checked here against the rows
in the database and the report is handed back to the model as the tool result —
it has to reconcile the difference before it can answer.

Checks are intentionally mechanical: coverage, counts, structure, spacing,
progression, sync state. Judgement calls (is this the right session for this
athlete?) stay with the coach.
"""

from datetime import date, timedelta

from app.garmin.sports import get_sport
from app.garmin.workouts import hr_zone_for, is_rest_day

# Intensity words that make a session "hard" for spacing purposes.
_HARD_INTENSITIES = {"tempo", "threshold", "interval", "hard", "sprint", "max"}

# A workout_type containing any of these implies a structured session; saving it
# with no steps means the breakdown never reaches the watch.
_STRUCTURE_WORDS = ("interval", "repeat", " x ", "x ", "tempo", "threshold", "fartlek", "hill")

# Week-over-week running volume jump that warrants a flag.
_MAX_WEEKLY_JUMP_PCT = 25.0
# Consecutive build weeks allowed before a down week is expected.
_MAX_BUILD_WEEKS = 3


def _issue(code: str, severity: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "detail": detail}


def _steps_flat(steps: list | None) -> list[dict]:
    """All leaf steps, repeat blocks expanded one level."""
    out: list[dict] = []
    for node in steps or []:
        if not isinstance(node, dict):
            continue
        if node.get("steps"):
            out.extend(c for c in node["steps"] if isinstance(c, dict))
        else:
            out.append(node)
    return out


def _is_hard(workout: dict) -> bool:
    if is_rest_day(workout.get("type") or ""):
        return False
    for step in _steps_flat(workout.get("steps")):
        if (step.get("intensity") or "").strip().lower() in _HARD_INTENSITIES:
            return True
    return hr_zone_for(workout.get("type") or "") >= 4


def _check_coverage(
    workouts: list[dict],
    dates: list[date],
    requested_start: date | None,
    requested_end: date | None,
) -> list[dict]:
    issues: list[dict] = []
    if not dates:
        return [_issue("empty_plan", "error", "The plan contains no workouts.")]

    start = requested_start or dates[0]
    end = requested_end or dates[-1]

    if requested_start and dates[0] > requested_start:
        issues.append(
            _issue(
                "starts_late",
                "error",
                f"Requested start {requested_start.isoformat()} but the first workout is {dates[0].isoformat()}.",
            )
        )
    if requested_end and dates[-1] < requested_end:
        missing_days = (requested_end - dates[-1]).days
        issues.append(
            _issue(
                "ends_early",
                "error",
                f"Requested through {requested_end.isoformat()} but the plan stops at "
                f"{dates[-1].isoformat()} — {missing_days} day(s) unplanned. Save the rest of the range.",
            )
        )

    scheduled = set(dates)
    gaps = [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
        if (start + timedelta(days=offset)) not in scheduled
    ]
    if gaps:
        issues.append(
            _issue(
                "days_without_entry",
                "warning",
                f"{len(gaps)} day(s) inside the plan range have no entry at all "
                f"(first: {gaps[0]}). Rest days should be saved explicitly.",
            )
        )
    if len(scheduled) != len(dates):
        issues.append(_issue("duplicate_dates", "error", "Two workouts share the same date."))
    return issues


def _check_structure(workouts: list[dict]) -> list[dict]:
    issues: list[dict] = []
    for w in workouts:
        wtype = (w.get("type") or "").lower()
        if is_rest_day(wtype):
            continue
        sport = get_sport(w.get("sport"))
        steps = w.get("steps") or []
        flat = _steps_flat(steps)

        if sport.key == "strength":
            if not any(s.get("exercise") for s in flat):
                issues.append(
                    _issue(
                        "strength_without_exercises",
                        "error",
                        f"{w['date']}: strength session '{w.get('type')}' has no exercise steps — "
                        "it will reach the watch as an empty session.",
                    )
                )
            for s in flat:
                if s.get("exercise") and not s.get("reps"):
                    issues.append(
                        _issue(
                            "exercise_without_reps",
                            "warning",
                            f"{w['date']}: exercise '{s['exercise']}' has no reps.",
                        )
                    )
            continue

        if not steps and any(word in wtype for word in _STRUCTURE_WORDS):
            issues.append(
                _issue(
                    "structured_session_without_steps",
                    "error",
                    f"{w['date']}: '{w.get('type')}' implies structure but was saved flat — "
                    "the breakdown never reaches the watch.",
                )
            )
        if sport.target == "hr" and flat and not any(s.get("intensity") for s in flat):
            issues.append(
                _issue(
                    "steps_without_intensity",
                    "warning",
                    f"{w['date']}: no step carries an intensity, so every step silently "
                    "defaults to an easy heart-rate target.",
                )
            )
        if w.get("pace_s_per_km") and not steps:
            issues.append(
                _issue(
                    "pace_as_target",
                    "warning",
                    f"{w['date']}: saved with a pace target. Heart rate is the actionable "
                    "target unless the athlete asked for pace.",
                )
            )
    return issues


def _check_spacing(workouts: list[dict]) -> list[dict]:
    """Hard days back to back, and long unbroken training streaks."""
    issues: list[dict] = []
    by_date = {date.fromisoformat(w["date"]): w for w in workouts}
    ordered = sorted(by_date)

    for day in ordered:
        nxt = day + timedelta(days=1)
        if nxt in by_date and _is_hard(by_date[day]) and _is_hard(by_date[nxt]):
            issues.append(
                _issue(
                    "hard_days_back_to_back",
                    "warning",
                    f"{day.isoformat()} and {nxt.isoformat()} are both hard sessions with no easy "
                    "day between them.",
                )
            )

    streak = 0
    longest = 0
    cursor = ordered[0] if ordered else None
    while cursor is not None and cursor <= ordered[-1]:
        w = by_date.get(cursor)
        training = w is not None and not is_rest_day(w.get("type") or "")
        streak = streak + 1 if training else 0
        longest = max(longest, streak)
        cursor += timedelta(days=1)
    if longest >= 7:
        issues.append(
            _issue(
                "no_rest_day",
                "warning",
                f"{longest} consecutive training days without a rest day.",
            )
        )
    return issues


def _weekly_breakdown(workouts: list[dict]) -> list[dict]:
    weeks: dict[tuple[int, int], dict] = {}
    for w in workouts:
        day = date.fromisoformat(w["date"])
        key = day.isocalendar()[:2]
        bucket = weeks.setdefault(
            key,
            {"week_start": (day - timedelta(days=day.isoweekday() - 1)).isoformat(),
             "sessions": 0, "runs": 0, "strength": 0, "rest_days": 0, "run_km": 0.0},
        )
        if is_rest_day(w.get("type") or ""):
            bucket["rest_days"] += 1
            continue
        bucket["sessions"] += 1
        sport = get_sport(w.get("sport")).key
        if sport == "running":
            bucket["runs"] += 1
            bucket["run_km"] += float(w.get("distance_m") or 0) / 1000
        elif sport == "strength":
            bucket["strength"] += 1
    out = [weeks[key] for key in sorted(weeks)]
    for bucket in out:
        bucket["run_km"] = round(bucket["run_km"], 1)
    return out


def _check_progression(weeks: list[dict]) -> list[dict]:
    issues: list[dict] = []
    build_streak = 0
    for prior, current in zip(weeks, weeks[1:]):
        if prior["run_km"] and current["run_km"]:
            change = (current["run_km"] - prior["run_km"]) / prior["run_km"] * 100
            if change > _MAX_WEEKLY_JUMP_PCT:
                issues.append(
                    _issue(
                        "volume_jump",
                        "warning",
                        f"Week of {current['week_start']}: running volume up {round(change)}% "
                        f"({prior['run_km']}km to {current['run_km']}km).",
                    )
                )
            build_streak = build_streak + 1 if change > -10 else 0
            if build_streak > _MAX_BUILD_WEEKS:
                issues.append(
                    _issue(
                        "no_deload",
                        "warning",
                        f"{build_streak + 1} straight weeks without an easier week "
                        f"(latest: week of {current['week_start']}).",
                    )
                )
                build_streak = 0
    return issues


def _check_sync(workouts: list[dict], today: date) -> list[dict]:
    pending = [
        w["date"]
        for w in workouts
        if not w.get("synced_to_garmin")
        and not is_rest_day(w.get("type") or "")
        and date.fromisoformat(w["date"]) >= today
    ]
    if not pending:
        return []
    return [
        _issue(
            "not_on_watch",
            "error",
            f"{len(pending)} upcoming session(s) are not on the watch (first: {pending[0]}). "
            "Do not tell the athlete they were synced.",
        )
    ]


def verify_workout(saved: dict, garmin: dict | None = None) -> dict:
    """Audit one saved session — the schedule_workout counterpart. Same rules as
    a plan's per-workout checks, plus whether the push actually landed."""
    workout = {**saved, "synced_to_garmin": bool((garmin or {}).get("synced"))}
    issues = _check_structure([workout])
    if not workout["synced_to_garmin"] and not is_rest_day(workout.get("type") or ""):
        reason = (garmin or {}).get("error") or (garmin or {}).get("skipped") or "unknown reason"
        issues.append(
            _issue(
                "not_on_watch",
                "error",
                f"The session was saved but did not reach the watch ({reason}). "
                "Do not tell the athlete it synced.",
            )
        )
    return {
        "ok": not any(i["severity"] == "error" for i in issues),
        "issues": issues,
        "saved": {
            "date": workout.get("date"),
            "sport": get_sport(workout.get("sport")).key,
            "type": workout.get("type"),
            "step_count": len(workout.get("steps") or []),
            "synced_to_garmin": workout["synced_to_garmin"],
        },
    }


def verify_plan(
    plan: dict | None,
    *,
    requested_start: str | None = None,
    requested_end: str | None = None,
    today: date | None = None,
) -> dict:
    """Audit a saved plan (as returned by get_training_plan) and report what is
    actually there. `requested_start`/`requested_end` are the range the athlete
    asked for, which is how a plan that stops halfway gets caught."""
    if not plan or not plan.get("workouts"):
        return {
            "ok": False,
            "issues": [_issue("no_active_plan", "error", "No active plan with workouts was found.")],
            "summary": {"workout_count": 0},
        }

    today = today or date.today()
    workouts = sorted(plan["workouts"], key=lambda w: w["date"])
    dates = [date.fromisoformat(w["date"]) for w in workouts]
    start = date.fromisoformat(requested_start) if requested_start else None
    end = date.fromisoformat(requested_end) if requested_end else None

    weeks = _weekly_breakdown(workouts)
    issues = [
        *_check_coverage(workouts, dates, start, end),
        *_check_structure(workouts),
        *_check_spacing(workouts),
        *_check_progression(weeks),
        *_check_sync(workouts, today),
    ]

    sports: dict[str, int] = {}
    for w in workouts:
        key = get_sport(w.get("sport")).key
        sports[key] = sports.get(key, 0) + 1

    longest_run_km = round(
        max((float(w.get("distance_m") or 0) for w in workouts), default=0.0) / 1000, 1
    )

    return {
        "ok": not any(i["severity"] == "error" for i in issues),
        "issues": issues,
        "summary": {
            "title": plan.get("title"),
            "workout_count": len(workouts),
            "first_date": workouts[0]["date"],
            "last_date": workouts[-1]["date"],
            "requested_range": (
                {"start": requested_start, "end": requested_end} if (start or end) else None
            ),
            "sports": sports,
            "rest_days": sum(1 for w in workouts if is_rest_day(w.get("type") or "")),
            "longest_run_km": longest_run_km,
            "synced_to_garmin": sum(1 for w in workouts if w.get("synced_to_garmin")),
            "weeks": weeks,
        },
    }
