"""Standing instructions the athlete has given the coach.

"Don't look at my sleep — I don't wear the watch at night" is not a passing
remark: it's a rule the coach has to keep honouring in every later
conversation. Prompt text alone can't guarantee that (the model can always
decide to mention sleep anyway), so a constraint does two things here: it is
injected into every system prompt, and the data it rules out is stripped from
what the coach can see at all.

Constraints live in the memories table with kind="constraint".
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Memory, User

# Topic -> the metric fields it covers, and the words that name it. Only data
# the coach can actually be blinded to belongs here; a constraint about
# something else (no workouts before 7am) is still honoured, just by the prompt
# rather than by filtering.
_TOPICS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "sleep": (("sleep_score", "sleep_duration_s"), ("sleep", "sleeping", "slept")),
    "hrv": (("hrv",), ("hrv", "heart rate variability")),
    "resting_hr": (("resting_hr",), ("resting hr", "resting heart rate", "rhr")),
    "stress": (("stress_avg",), ("stress",)),
    "body_battery": (("body_battery",), ("body battery",)),
    "vo2max": (("vo2max",), ("vo2max", "vo2 max")),
    "training_readiness": (("training_readiness",), ("training readiness", "readiness")),
    "weight": ((), ("weight", "weigh")),
    "weather": ((), ("weather", "forecast", "rain", "temperature")),
}

# A constraint is a prohibition when it reads like one. Without this, a plain
# fact that happens to mention sleep ("sleeps badly before races") would blind
# the coach to sleep data.
_NEGATIONS = (
    "don't",
    "dont",
    "do not",
    "never",
    "stop",
    "avoid",
    "ignore",
    "disregard",
    "skip",
    "leave out",
    "no longer",
    "not use",
    "without",
)


def _is_prohibition(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _NEGATIONS)


def suppressed_topics(constraints: list[str]) -> set[str]:
    """Topics the athlete has told the coach to stay away from."""
    out: set[str] = set()
    for text in constraints:
        if not _is_prohibition(text):
            continue
        lowered = text.lower()
        for topic, (_, words) in _TOPICS.items():
            if any(word in lowered for word in words):
                out.add(topic)
    return out


async def load_constraints(db: AsyncSession, user: User) -> list[dict]:
    """Every standing instruction, oldest first — all of them, never a recent
    slice: a rule the athlete set months ago still binds."""
    rows = await db.execute(
        select(Memory.id, Memory.content)
        .where(Memory.user_id == user.id, Memory.kind == "constraint")
        .order_by(Memory.created_at)
    )
    return [{"id": row.id, "text": row.content} for row in rows]


async def suppressed_topics_for(db: AsyncSession, user: User) -> set[str]:
    return suppressed_topics([c["text"] for c in await load_constraints(db, user)])


def filter_metrics(metrics: list[dict], suppressed: set[str]) -> list[dict]:
    """Blank out the fields the athlete ruled out, so the coach cannot use or
    quote them even if it wanted to."""
    if not suppressed:
        return metrics
    blocked = {field for topic in suppressed for field in _TOPICS.get(topic, ((), ()))[0]}
    if not blocked:
        return metrics
    return [{k: (None if k in blocked else v) for k, v in m.items()} for m in metrics]


def filter_reasons(reasons: list[str], suppressed: set[str]) -> list[str]:
    """Drop recovery explanations that lean on a suppressed signal — the flag
    itself can stand, but its reasoning must not quote data the athlete asked
    the coach not to look at."""
    if not suppressed:
        return reasons
    words = tuple(word for topic in suppressed for word in _TOPICS.get(topic, ((), ()))[1])
    return [r for r in reasons if not any(word in r.lower() for word in words)]


def format_constraint_block(constraints: list[dict], suppressed: set[str]) -> str:
    if not constraints:
        return ""
    lines = "\n".join(f"- id={c['id']} {c['text']}" for c in constraints)
    block = (
        "Standing instructions from the athlete — these override your defaults and stay in force "
        "until they say otherwise. Follow them without being asked again, and never argue them "
        "back:\n" + lines
    )
    if suppressed:
        block += (
            "\n\nData withheld at their request: "
            + ", ".join(sorted(suppressed))
            + ". Those fields are blanked out everywhere you can see them — do not comment on "
            "them, ask about them, or work around them."
        )
    return block
