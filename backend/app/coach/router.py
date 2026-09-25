"""Which agent answers this message.

One model doing everything means every "thanks" costs a full planning-capable
call with nineteen tool schemas attached, and every planning request competes
for attention with small talk. So a cheap classifier picks a lane first, and
each lane gets its own model, prompt and slice of the toolset.

Lanes are deliberately few — a taxonomy the classifier can't get wrong in an
interesting way is worth more than a precise one.
"""

import logging
from dataclasses import dataclass, field

from openai import AsyncOpenAI, OpenAIError

from app.config import settings

logger = logging.getLogger(__name__)

# Read-only view of the athlete's world: enough to answer questions, not enough
# to change anything.
_READ_TOOLS = (
    "get_daily_metrics",
    "get_recent_activities",
    "get_goals",
    "get_training_plan",
    "get_recovery_status",
    "get_weather_forecast",
    "search_memory",
)

# Always available, whatever the lane: the athlete can ask to be left alone (or
# tell you something worth keeping) in the middle of any conversation.
_ALWAYS_TOOLS = ("store_memory", "pause_check_ins", "resume_check_ins")


@dataclass(frozen=True)
class Lane:
    name: str
    model: str
    # None means every tool; a tuple restricts the schemas passed to the model.
    tools: tuple[str, ...] | None = None
    prompt_suffix: str = ""
    # Lanes that never write are cheap to retry and safe to get wrong.
    read_only: bool = False
    aliases: tuple[str, ...] = field(default=())


def _small_model() -> str:
    return settings.llm_router_model or settings.llm_model


def _vision_model() -> str:
    return settings.llm_vision_model or settings.llm_model


# Verbs that must never end up in a read-only lane, whatever the classifier
# says: a misroute here would have the coach explaining it can't do something
# it can.
_WRITE_INTENT = (
    "plan",
    "schedule",
    "program",
    "book",
    "add ",
    "create",
    "build",
    "set a goal",
    "save",
    "move ",
    "reschedul",
    "cancel",
    "delete",
    "remove",
    "change",
    "update",
)

_CLASSIFIER_PROMPT = """You route a running coach's incoming messages. Answer with exactly one word.

coach — anything that changes something or needs real coaching judgement: training plans, \
scheduling or moving workouts, goals, injuries, "what should I do", advice, analysis of how \
training is going.
quick — a factual question about data already recorded: a number, a date, what's on the calendar, \
recent activities, current recovery, the weather.
smalltalk — greetings, thanks, acknowledgements, jokes, anything with no informational content.

When unsure, answer coach. Answer with one word and nothing else."""

LANES: dict[str, Lane] = {}


def _build_lanes() -> dict[str, Lane]:
    return {
        "coach": Lane(name="coach", model=settings.llm_model),
        "quick": Lane(
            name="quick",
            model=_small_model(),
            tools=_READ_TOOLS + _ALWAYS_TOOLS,
            read_only=True,
            prompt_suffix=(
                "\n\nThis turn is a straight factual question about data you already have. "
                "Answer it in one or two sentences with the actual numbers, and stop. No plan, "
                "no coaching advice, no follow-up offer unless they asked for one. If answering "
                "properly would need you to change a plan, a goal or a workout, say you'll sort "
                "it out and let them ask again — do not pretend to have done it."
            ),
        ),
        "smalltalk": Lane(
            name="smalltalk",
            model=_small_model(),
            tools=_ALWAYS_TOOLS,
            read_only=True,
            prompt_suffix=(
                "\n\nThis turn is small talk. Reply in one short, warm line. Don't pull up data, "
                "don't coach, don't suggest a workout."
            ),
        ),
        "vision": Lane(
            name="vision",
            model=_vision_model(),
            tools=_READ_TOOLS + _ALWAYS_TOOLS,
            read_only=True,
            prompt_suffix=(
                "\n\nThe athlete has sent you an image — typically a workout written down, a gym "
                "whiteboard, a screenshot from another app, a treadmill or watch display, or a "
                "race result.\n\n"
                "Read it and say what you see in one or two lines: the sport, and the actual "
                "structure (exercises with sets and reps, intervals with distances and "
                "recoveries, distance, time, pace, heart rate — whatever is legible), using their "
                "numbers, not rounded or improved ones. Never invent a value you cannot read; say "
                "which part is unclear instead, and ask only if it matters.\n\n"
                "Then offer — in one short line — to put it on their watch, and stop. You must "
                "not schedule anything this turn: a misread photo becomes a wrong session on "
                "someone's watch. Wait for them to say yes.\n\n"
                "If the image has nothing to do with training, say so briefly and don't analyse it."
            ),
        ),
    }


def lane(name: str) -> Lane:
    """The lane by name, rebuilt each call so a settings change is picked up."""
    lanes = _build_lanes()
    return lanes.get(name, lanes["coach"])


def _wants_write(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _WRITE_INTENT)


async def classify(text: str, *, has_image: bool = False) -> str:
    """Pick a lane for this message.

    An image decides on its own — no classifier call needed. Everything else
    gets one cheap completion, with the coach lane as the answer to every kind
    of doubt: routing a planning request to a read-only lane is a visible
    failure, while routing chit-chat to the coach only costs money.
    """
    if has_image:
        return "vision"

    stripped = (text or "").strip()
    if not stripped:
        return "coach"
    if _wants_write(stripped):
        return "coach"

    client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
    try:
        response = await client.chat.completions.create(
            model=_small_model(),
            messages=[
                {"role": "system", "content": _CLASSIFIER_PROMPT},
                {"role": "user", "content": stripped[:1000]},
            ],
            max_tokens=4,
            temperature=0,
        )
        choice = (response.choices[0].message.content or "").strip().lower()
    except OpenAIError:
        logger.exception("Lane classification failed — falling back to coach")
        return "coach"

    for name in ("smalltalk", "quick", "coach"):
        if name in choice:
            return name
    logger.warning("Unrecognized lane %r from classifier — using coach", choice)
    return "coach"
