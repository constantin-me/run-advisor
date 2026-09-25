"""Lane routing: which agent (and which model) answers a message.

The expensive failure is a planning request landing in a read-only lane, so
most of these pin down the cases that must never be classified away.
"""

from types import SimpleNamespace

import pytest

import app.coach.router as router
from app.coach.agent import _tools_for, _user_turn


def _fake_client(answer: str, calls: list | None = None):
    class FakeCompletions:
        async def create(self, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            message = SimpleNamespace(content=answer)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    return FakeClient


@pytest.mark.asyncio
async def test_an_image_routes_to_vision_without_asking_the_classifier(monkeypatch):
    def explode(**kwargs):  # the classifier must not be called at all
        raise AssertionError("classifier should not run for an image")

    monkeypatch.setattr(router, "AsyncOpenAI", explode)
    assert await router.classify("what do you make of this?", has_image=True) == "vision"


@pytest.mark.parametrize(
    "text",
    [
        "build me a 4 week plan",
        "schedule intervals on Tuesday",
        "can you move Thursday's run",
        "delete the long run this weekend",
        "set a goal of sub-40 10k",
    ],
)
@pytest.mark.asyncio
async def test_write_intent_never_leaves_the_coach_lane(monkeypatch, text):
    # Even if the classifier insists otherwise.
    monkeypatch.setattr(router, "AsyncOpenAI", _fake_client("smalltalk"))
    assert await router.classify(text) == "coach"


@pytest.mark.asyncio
async def test_classifier_answer_is_used_for_everything_else(monkeypatch):
    monkeypatch.setattr(router, "AsyncOpenAI", _fake_client("quick"))
    assert await router.classify("what was my resting HR yesterday?") == "quick"

    monkeypatch.setattr(router, "AsyncOpenAI", _fake_client("smalltalk"))
    assert await router.classify("morning!") == "smalltalk"


@pytest.mark.asyncio
async def test_unusable_classifier_answers_fall_back_to_coach(monkeypatch):
    monkeypatch.setattr(router, "AsyncOpenAI", _fake_client("banana"))
    assert await router.classify("how did last week go?") == "coach"

    monkeypatch.setattr(router, "AsyncOpenAI", _fake_client(""))
    assert await router.classify("how did last week go?") == "coach"


@pytest.mark.asyncio
async def test_classifier_failure_falls_back_to_coach(monkeypatch):
    from openai import OpenAIError

    class Boom:
        def __init__(self, **kwargs):
            async def create(**_):
                raise OpenAIError("down")

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setattr(router, "AsyncOpenAI", Boom)
    assert await router.classify("how did last week go?") == "coach"


@pytest.mark.asyncio
async def test_empty_message_goes_to_coach(monkeypatch):
    monkeypatch.setattr(router, "AsyncOpenAI", _fake_client("smalltalk"))
    assert await router.classify("   ") == "coach"


@pytest.mark.asyncio
async def test_classifier_runs_on_the_small_model(monkeypatch):
    calls: list = []
    monkeypatch.setattr(router, "AsyncOpenAI", _fake_client("quick", calls))
    monkeypatch.setattr(router.settings, "llm_router_model", "tiny-model")
    await router.classify("what's my VO2max?")
    assert calls[0]["model"] == "tiny-model"


def test_read_only_lanes_cannot_reach_a_writing_tool():
    writes = {"save_training_plan", "schedule_workout", "save_goal", "complete_goal"}
    for name in ("quick", "smalltalk", "vision"):
        names = {t["function"]["name"] for t in _tools_for(router.lane(name))}
        assert not (names & writes), f"{name} lane can write"


def test_the_coach_lane_keeps_every_tool():
    from app.coach.tools import TOOL_SCHEMAS

    assert len(_tools_for(router.lane("coach"))) == len(TOOL_SCHEMAS)


def test_every_lane_can_still_be_told_to_stop():
    for name in ("coach", "quick", "smalltalk", "vision"):
        names = {t["function"]["name"] for t in _tools_for(router.lane(name))}
        assert "pause_check_ins" in names


def test_vision_lane_uses_the_vision_model(monkeypatch):
    monkeypatch.setattr(router.settings, "llm_vision_model", "some-vision-model")
    assert router.lane("vision").model == "some-vision-model"
    monkeypatch.setattr(router.settings, "llm_vision_model", None)
    assert router.lane("vision").model == router.settings.llm_model


def test_unknown_lane_name_falls_back_to_coach():
    assert router.lane("nonsense").name == "coach"


def test_image_turn_carries_text_and_picture():
    turn = _user_turn("what do you think?", "data:image/jpeg;base64,AAA")
    assert turn["content"][0] == {"type": "text", "text": "what do you think?"}
    assert turn["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_turn_without_a_caption_is_just_the_picture():
    turn = _user_turn("", "data:image/jpeg;base64,AAA")
    assert len(turn["content"]) == 1
    assert turn["content"][0]["type"] == "image_url"


def test_text_turn_is_unchanged():
    assert _user_turn("hello", None) == {"role": "user", "content": "hello"}
