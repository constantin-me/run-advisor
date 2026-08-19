"""Sport registry — one place that knows how every supported sport is built,
targeted and uploaded.

The coach used to be able to create running workouts only, so a gym block came
out as a run with fake sub-steps. Garmin (and the client library) support far
more: each sport differs in three ways that matter to us — the ``sportType``
block on the payload, which upload endpoint takes it, and whether its steps can
carry a heart-rate target at all. Those three live here so
``app/garmin/workouts.py`` stays about step building.
"""

from dataclasses import dataclass

from garminconnect.workout import (
    BaseWorkout,
    CyclingWorkout,
    HikingWorkout,
    RunningWorkout,
    StrengthWorkout,
    SwimmingWorkout,
    WalkingWorkout,
)


@dataclass(frozen=True)
class Sport:
    """One sport the coach can schedule.

    cls: typed workout model from the library, or None when the library has no
        class for it — then we build a plain BaseWorkout with an explicit
        sportType, which is all Garmin actually requires.
    upload: name of the typed upload method, or None to go through the generic
        ``upload_workout(json)`` endpoint.
    target: "hr" when steps should carry a heart-rate target, "none" when a
        target is meaningless or misleading (lifting, yoga, in-water HR).
    step_style: how a step list for this sport is shaped and how its duration
        is estimated — "endurance" (distance/time), "strength" (reps), "timed".
    """

    key: str
    type_id: int
    type_key: str
    display_order: int
    cls: type | None
    upload: str | None
    target: str = "hr"
    step_style: str = "endurance"
    label: str = ""

    @property
    def sport_type(self) -> dict:
        """The segment/workout sportType block. Ids and display orders are the
        library's own per-class values (they are not all equal — swimming is
        id 4 / order 3, walking 17, hiking 18)."""
        return {
            "sportTypeId": self.type_id,
            "sportTypeKey": self.type_key,
            "displayOrder": self.display_order,
        }

    @property
    def workout_cls(self) -> type:
        return self.cls or BaseWorkout


SPORTS: dict[str, Sport] = {
    "running": Sport("running", 1, "running", 1, RunningWorkout, "upload_running_workout", label="Run"),
    "cycling": Sport("cycling", 2, "cycling", 2, CyclingWorkout, "upload_cycling_workout", label="Bike"),
    "other": Sport("other", 3, "other", 3, None, None, target="none", step_style="timed", label="Other"),
    # HR straps are unreliable in water and Garmin swim workouts are built
    # around distance/stroke, not bpm.
    "swimming": Sport(
        "swimming", 4, "swimming", 3, SwimmingWorkout, "upload_swimming_workout", target="none", label="Swim"
    ),
    "strength": Sport(
        "strength",
        5,
        "strength_training",
        5,
        StrengthWorkout,
        "upload_strength_workout",
        target="none",
        step_style="strength",
        label="Strength",
    ),
    "cardio": Sport("cardio", 6, "cardio_training", 6, None, None, step_style="timed", label="Cardio"),
    "yoga": Sport("yoga", 7, "yoga", 7, None, None, target="none", step_style="timed", label="Yoga"),
    "pilates": Sport("pilates", 8, "pilates", 8, None, None, target="none", step_style="timed", label="Pilates"),
    "hiit": Sport("hiit", 9, "hiit", 9, None, None, step_style="timed", label="HIIT"),
    "walking": Sport("walking", 17, "walking", 17, WalkingWorkout, "upload_walking_workout", label="Walk"),
    "hiking": Sport("hiking", 18, "hiking", 18, HikingWorkout, "upload_hiking_workout", label="Hike"),
    "mobility": Sport(
        "mobility", 11, "mobility", 11, None, None, target="none", step_style="timed", label="Mobility"
    ),
}

DEFAULT_SPORT = "running"

# Aliases for what a coach (or an old row) might say instead of our key.
_ALIASES: dict[str, str] = {
    "run": "running",
    "bike": "cycling",
    "biking": "cycling",
    "cycle": "cycling",
    "ride": "cycling",
    "swim": "swimming",
    "strength_training": "strength",
    "gym": "strength",
    "weights": "strength",
    "lifting": "strength",
    "walk": "walking",
    "hike": "hiking",
    "cardio_training": "cardio",
    "elliptical": "cardio",
    "stretching": "mobility",
    "": DEFAULT_SPORT,
}


def get_sport(key: str | None) -> Sport:
    """Resolve a sport key to a Sport. Anything unrecognized falls back to
    running — the sport every existing row implicitly is."""
    raw = (key or "").strip().lower().replace(" ", "_")
    raw = _ALIASES.get(raw, raw)
    return SPORTS.get(raw, SPORTS[DEFAULT_SPORT])


def sport_keys() -> list[str]:
    return list(SPORTS)


def upload_callable(client, sport: Sport):
    """The client method that takes a built workout for this sport.

    Falls back to the generic JSON endpoint both for sports the library has no
    typed uploader for, and for a library version older than the typed method —
    a missing helper degrades instead of raising."""
    method = getattr(client, sport.upload, None) if sport.upload else None
    if method is not None:
        return method

    def _upload_generic(built):
        payload = built.to_dict() if hasattr(built, "to_dict") else built
        return client.upload_workout(payload)

    return _upload_generic


# Garmin activity type strings -> our sport keys. Ordered most specific first:
# "trail_running" must match running, "indoor_cycling" cycling, and so on.
_ACTIVITY_MATCHERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("running", ("run", "treadmill_running", "track_running")),
    ("cycling", ("cycl", "bik", "ride", "spinning")),
    ("swimming", ("swim",)),
    ("strength", ("strength", "weight", "bouldering")),
    ("hiit", ("hiit",)),
    ("yoga", ("yoga",)),
    ("pilates", ("pilates",)),
    ("mobility", ("mobility", "stretch", "breathwork")),
    ("hiking", ("hiking",)),
    ("walking", ("walk",)),
    ("cardio", ("cardio", "elliptical", "indoor_climbing", "rowing", "fitness_equipment")),
)


def classify_activity(activity_type: str | None) -> str:
    """Bucket a synced activity's Garmin type into a sport key. Used by progress
    analysis so a gym or bike block isn't read as a lost training week."""
    lowered = (activity_type or "").strip().lower()
    for sport_key, needles in _ACTIVITY_MATCHERS:
        if any(needle in lowered for needle in needles):
            return sport_key
    return "other"
