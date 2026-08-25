"""Tests for the plan verifier — the thing that stops the coach reporting a
plan it didn't fully save, or a sync that didn't happen."""

from datetime import date, timedelta

import pytest

from app.coach.verify import verify_plan, verify_workout

TODAY = date(2026, 8, 24)  # a Monday


def _run(day: date, *, distance_km=8.0, wtype="easy run", steps=None, synced=True, sport="running"):
    return {
        "date": day.isoformat(),
        "sport": sport,
        "type": wtype,
        "description": None,
        "steps": steps if steps is not None else [{"kind": "run", "distance_m": distance_km * 1000, "intensity": "easy"}],
        "distance_m": distance_km * 1000,
        "pace_s_per_km": None,
        "done": False,
        "synced_to_garmin": synced,
    }


def _plan(workouts, title="Test plan"):
    return {"id": 1, "title": title, "workouts": workouts}


def _codes(report):
    return {i["code"] for i in report["issues"]}


def test_plan_stopping_short_of_the_requested_range_is_an_error():
    workouts = [_run(TODAY + timedelta(days=n)) for n in range(7)]
    report = verify_plan(
        _plan(workouts),
        requested_start=TODAY.isoformat(),
        requested_end=(TODAY + timedelta(days=27)).isoformat(),
        today=TODAY,
    )
    assert report["ok"] is False
    assert "ends_early" in _codes(report)
    assert report["summary"]["workout_count"] == 7


def test_clean_plan_passes():
    workouts = []
    for week in range(2):
        for offset, (wtype, km) in enumerate(
            [("easy run", 8), ("rest", 0), ("tempo run", 10), ("rest", 0), ("easy run", 8), ("rest", 0), ("long run", 16)]
        ):
            day = TODAY + timedelta(days=week * 7 + offset)
            if wtype == "rest":
                workouts.append({**_run(day, distance_km=0, wtype="rest"), "steps": None, "distance_m": None})
            else:
                workouts.append(_run(day, distance_km=km, wtype=wtype))
    report = verify_plan(
        _plan(workouts),
        requested_start=TODAY.isoformat(),
        requested_end=(TODAY + timedelta(days=13)).isoformat(),
        today=TODAY,
    )
    assert report["ok"] is True, report["issues"]
    assert report["summary"]["sports"] == {"running": 14}
    assert report["summary"]["rest_days"] == 6
    assert report["summary"]["longest_run_km"] == 16.0


def test_unsynced_upcoming_session_is_reported():
    workouts = [_run(TODAY + timedelta(days=1), synced=False)]
    report = verify_plan(_plan(workouts), today=TODAY)
    assert "not_on_watch" in _codes(report)
    assert report["ok"] is False


def test_past_unsynced_sessions_are_not_flagged():
    workouts = [_run(TODAY - timedelta(days=3), synced=False), _run(TODAY, synced=True)]
    report = verify_plan(_plan(workouts), today=TODAY)
    assert "not_on_watch" not in _codes(report)


def test_structured_session_saved_flat_is_an_error():
    workouts = [{**_run(TODAY, wtype="4 x 1km intervals"), "steps": None}]
    report = verify_plan(_plan(workouts), today=TODAY)
    assert "structured_session_without_steps" in _codes(report)


def test_strength_day_without_exercises_is_an_error():
    workouts = [_run(TODAY, sport="strength", wtype="push day", steps=[{"kind": "run", "duration_s": 600}])]
    report = verify_plan(_plan(workouts), today=TODAY)
    assert "strength_without_exercises" in _codes(report)


def test_strength_day_with_exercises_passes_structure_checks():
    steps = [{"repeat": 4, "steps": [
        {"kind": "exercise", "exercise": "Barbell Bench Press", "reps": 10},
        {"kind": "rest", "duration_s": 120},
    ]}]
    workouts = [_run(TODAY, sport="strength", wtype="push day", steps=steps, distance_km=0)]
    report = verify_plan(_plan(workouts), today=TODAY)
    assert _codes(report) == set()


def test_back_to_back_hard_days_flagged():
    workouts = [_run(TODAY, wtype="tempo run"), _run(TODAY + timedelta(days=1), wtype="threshold intervals",
                                                     steps=[{"kind": "run", "distance_m": 1000, "intensity": "threshold"}])]
    report = verify_plan(_plan(workouts), today=TODAY)
    assert "hard_days_back_to_back" in _codes(report)


def test_volume_jump_between_weeks_flagged():
    workouts = [_run(TODAY + timedelta(days=n), distance_km=5) for n in range(0, 7, 2)]
    workouts += [_run(TODAY + timedelta(days=n), distance_km=15) for n in range(7, 14, 2)]
    report = verify_plan(_plan(workouts), today=TODAY)
    assert "volume_jump" in _codes(report)
    assert len(report["summary"]["weeks"]) == 2


def test_pace_target_without_steps_is_flagged():
    workout = {**_run(TODAY), "steps": None, "pace_s_per_km": 300}
    report = verify_plan(_plan([workout]), today=TODAY)
    assert "pace_as_target" in _codes(report)


def test_missing_plan_is_not_ok():
    report = verify_plan(None)
    assert report["ok"] is False
    assert "no_active_plan" in _codes(report)


@pytest.mark.parametrize("garmin,expect_ok", [({"synced": True}, True), ({"synced": False, "error": "boom"}, False)])
def test_single_workout_verification_tracks_the_push(garmin, expect_ok):
    saved = {
        "date": TODAY.isoformat(),
        "sport": "running",
        "type": "easy run",
        "steps": [{"kind": "run", "distance_m": 8000, "intensity": "easy"}],
        "distance_m": 8000,
        "pace_s_per_km": None,
    }
    report = verify_workout(saved, garmin)
    assert report["ok"] is expect_ok
    assert report["saved"]["synced_to_garmin"] is garmin["synced"]
