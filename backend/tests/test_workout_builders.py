"""Payload-shape tests for the workout builders.

These are the only place the exact JSON Garmin receives is pinned down: the
push path itself needs a live account, so a wrong stepOrder or a missing
childStepId would otherwise only show up as a mangled workout on someone's
watch.
"""

import pytest

from app.garmin.sports import classify_activity, get_sport, upload_callable
from app.garmin.workouts import _resolve_exercise, build_workout


def _steps_of(built) -> list:
    return built.to_dict()["workoutSegments"][0]["workoutSteps"]


# --- sport registry -------------------------------------------------------


def test_unknown_sport_falls_back_to_running():
    assert get_sport("underwater basket weaving").key == "running"
    assert get_sport(None).key == "running"
    assert get_sport("gym").key == "strength"
    assert get_sport("BIKE").key == "cycling"


@pytest.mark.parametrize(
    "key,type_id,type_key",
    [
        ("running", 1, "running"),
        ("cycling", 2, "cycling"),
        ("strength", 5, "strength_training"),
        ("yoga", 7, "yoga"),
        ("walking", 17, "walking"),
    ],
)
def test_sport_type_lands_on_payload(key, type_id, type_key):
    built = build_workout("session", None, None, None, sport=key, steps=[{"kind": "run", "duration_s": 600}])
    payload = built.to_dict()
    assert payload["sportType"]["sportTypeId"] == type_id
    assert payload["sportType"]["sportTypeKey"] == type_key
    assert payload["workoutSegments"][0]["sportType"]["sportTypeId"] == type_id


def test_upload_callable_prefers_typed_method_then_generic():
    class FakeClient:
        def upload_running_workout(self, workout):
            return "typed"

        def upload_workout(self, payload):
            return "generic"

    client = FakeClient()
    assert upload_callable(client, get_sport("running"))(None) == "typed"
    # yoga has no typed uploader in the library -> generic JSON endpoint
    built = build_workout("flow", None, None, None, sport="yoga", steps=[{"kind": "rest", "duration_s": 600}])
    assert upload_callable(client, get_sport("yoga"))(built) == "generic"


# --- running (regression guard) -------------------------------------------


def test_running_intervals_keep_flat_step_order_and_child_ids():
    steps = [
        {"kind": "warmup", "duration_s": 600, "intensity": "easy"},
        {"repeat": 3, "steps": [
            {"kind": "run", "distance_m": 200, "intensity": "sprint"},
            {"kind": "recovery", "distance_m": 200, "intensity": "easy"},
        ]},
        {"kind": "cooldown", "duration_s": 600, "intensity": "easy"},
    ]
    built = build_workout("sprint intervals", None, None, None, steps=steps, sport="running")
    warmup, group, cooldown = _steps_of(built)

    assert warmup["stepOrder"] == 1
    assert group["stepOrder"] == 2
    assert group["numberOfIterations"] == 3
    assert group["endCondition"]["conditionTypeKey"] == "iterations"
    assert [c["stepOrder"] for c in group["workoutSteps"]] == [3, 4]
    assert {c["childStepId"] for c in group["workoutSteps"]} == {group["childStepId"]}
    assert cooldown["stepOrder"] == 5
    # HR is the governor for running, so every non-rest step carries a target.
    assert warmup["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"


def test_running_without_steps_still_builds_a_single_step():
    built = build_workout("easy run", None, 5000, 300, sport="running")
    steps = _steps_of(built)
    assert len(steps) == 1
    assert steps[0]["endCondition"]["conditionTypeKey"] == "distance"
    assert steps[0]["endConditionValue"] == 5000.0


# --- strength -------------------------------------------------------------


def test_strength_set_shape():
    steps = [
        {"repeat": 4, "steps": [
            {"kind": "exercise", "exercise": "Barbell Bench Press", "reps": 10, "weight_kg": 60},
            {"kind": "rest", "duration_s": 120},
        ]}
    ]
    built = build_workout("push day", None, None, None, steps=steps, sport="strength")
    payload = built.to_dict()
    assert payload["sportType"]["sportTypeKey"] == "strength_training"

    (group,) = payload["workoutSegments"][0]["workoutSteps"]
    assert group["numberOfIterations"] == 4
    exercise, rest = group["workoutSteps"]

    assert exercise["category"] == "BENCH_PRESS"
    assert exercise["exerciseName"] == "BARBELL_BENCH_PRESS"
    assert exercise["endCondition"]["conditionTypeKey"] == "reps"
    assert exercise["endConditionValue"] == 10.0
    # Garmin stores weight in grams.
    assert exercise["weightValue"] == 60_000.0
    assert exercise["childStepId"] == group["childStepId"]
    assert [exercise["stepOrder"], rest["stepOrder"]] == [2, 3]

    assert rest["stepType"]["stepTypeKey"] == "rest"
    assert rest["endConditionValue"] == 120.0


def test_strength_steps_carry_no_heart_rate_target():
    steps = [{"kind": "exercise", "exercise": "Barbell Deadlift", "reps": 5}]
    built = build_workout("deadlifts", None, None, None, steps=steps, sport="strength")
    (step,) = _steps_of(built)
    assert step["targetType"]["workoutTargetTypeKey"] == "no.target"
    assert "weightValue" not in step  # no load given -> athlete picks


def test_unknown_exercise_degrades_instead_of_raising():
    category, name = _resolve_exercise("Interdimensional Kettlebell Yeet")
    assert category == "TOTAL_BODY"
    assert name == ""

    steps = [{"kind": "exercise", "exercise": "Interdimensional Kettlebell Yeet", "reps": 8}]
    built = build_workout("mystery", None, None, None, steps=steps, sport="strength")
    (step,) = _steps_of(built)
    assert step["category"] == "TOTAL_BODY"
    # The requested name survives in the description so the athlete still knows
    # what they asked for.
    assert "Yeet" in step["description"]


@pytest.mark.parametrize(
    "written,category",
    [
        ("bench press", "BENCH_PRESS"),
        ("Barbell Bench Press", "BENCH_PRESS"),
        # plural vs Garmin's singular, and a hyphen it doesn't write
        ("Pull-ups", "PULL_UP"),
        ("Sit Ups", "SIT_UP"),
        # compound word split differently on each side ("Lat Pull-down")
        ("Lat Pulldown", "PULL_UP"),
        # trailing "ss" must survive de-pluralization
        ("Leg Press", "SQUAT"),
        ("Barbell Squat", "SQUAT"),
    ],
)
def test_exercise_names_resolve_the_way_a_coach_writes_them(written, category):
    assert _resolve_exercise(written)[0] == category


def test_strength_duration_estimate_counts_reps_and_rest():
    steps = [
        {"repeat": 3, "steps": [
            {"kind": "exercise", "exercise": "Barbell Squat", "reps": 10},
            {"kind": "rest", "duration_s": 60},
        ]}
    ]
    built = build_workout("legs", None, None, None, steps=steps, sport="strength")
    # 3 x (10 reps x 3s + 60s rest)
    assert built.to_dict()["estimatedDurationInSecs"] == 270


# --- other sports ---------------------------------------------------------


def test_yoga_uses_base_workout_without_hr_targets():
    steps = [{"kind": "rest", "duration_s": 1800}]
    built = build_workout("evening flow", None, None, None, steps=steps, sport="yoga")
    payload = built.to_dict()
    assert payload["sportType"]["sportTypeKey"] == "yoga"
    assert payload["workoutSegments"][0]["workoutSteps"][0]["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_cycling_keeps_heart_rate_targets():
    steps = [{"kind": "run", "duration_s": 1200, "intensity": "tempo"}]
    built = build_workout("bike tempo", None, None, None, steps=steps, sport="cycling")
    (step,) = _steps_of(built)
    assert step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"


@pytest.mark.parametrize(
    "activity_type,expected",
    [
        ("trail_running", "running"),
        ("treadmill_running", "running"),
        ("indoor_cycling", "cycling"),
        ("lap_swimming", "swimming"),
        ("strength_training", "strength"),
        ("yoga", "yoga"),
        ("walking", "walking"),
        (None, "other"),
    ],
)
def test_activity_classification(activity_type, expected):
    assert classify_activity(activity_type) == expected
