"""Tests for standing instructions — the rules the athlete gives the coach.

The point of these is that a "don't look at my sleep" is honoured by the data
layer, not by the model's goodwill.
"""

import pytest

from app.coach.constraints import (
    filter_metrics,
    filter_reasons,
    format_constraint_block,
    suppressed_topics,
)

SLEEP_RULE = "Don't look at my sleep — I don't wear the watch at night"


def _metrics():
    return [
        {
            "date": "2026-08-24",
            "sleep_score": 71,
            "sleep_duration_s": 25200.0,
            "hrv": 45,
            "resting_hr": 48,
            "stress_avg": 30,
            "body_battery": 80,
        }
    ]


@pytest.mark.parametrize(
    "text,expected",
    [
        (SLEEP_RULE, {"sleep"}),
        ("stop mentioning my weight", {"weight"}),
        ("never bring up HRV again", {"hrv"}),
        ("ignore the weather, I run indoors", {"weather"}),
        ("don't use resting heart rate or body battery", {"resting_hr", "body_battery"}),
    ],
)
def test_prohibitions_are_recognized(text, expected):
    assert suppressed_topics([text]) == expected


@pytest.mark.parametrize(
    "text",
    [
        # A fact that merely mentions a topic must not blind the coach to it.
        "sleeps badly the night before a race",
        "wants to get faster over 10k",
        "weight is currently 74kg",
    ],
)
def test_plain_facts_do_not_suppress_anything(text):
    assert suppressed_topics([text]) == set()


def test_suppressed_fields_are_blanked_not_dropped():
    filtered = filter_metrics(_metrics(), {"sleep"})
    assert filtered[0]["sleep_score"] is None
    assert filtered[0]["sleep_duration_s"] is None
    # Untouched signals stay intact.
    assert filtered[0]["hrv"] == 45
    assert filtered[0]["resting_hr"] == 48
    # The original is not mutated.
    assert _metrics()[0]["sleep_score"] == 71


def test_no_constraints_leaves_metrics_untouched():
    metrics = _metrics()
    assert filter_metrics(metrics, set()) is metrics


def test_topic_without_metric_fields_does_not_blank_anything():
    # "weight" is honoured by the prompt; there is no weight field to strip.
    assert filter_metrics(_metrics(), {"weight"}) == _metrics()


def test_recovery_reasons_stop_quoting_suppressed_signals():
    reasons = ["sleep score 42 (poor)", "HRV 22% below baseline", "resting HR +6"]
    assert filter_reasons(reasons, {"sleep"}) == [
        "HRV 22% below baseline",
        "resting HR +6",
    ]


def test_constraint_block_lists_ids_and_withheld_data():
    block = format_constraint_block([{"id": 3, "text": SLEEP_RULE}], {"sleep"})
    assert "id=3" in block
    assert SLEEP_RULE in block
    assert "Data withheld at their request: sleep" in block


def test_no_constraints_means_no_block():
    assert format_constraint_block([], set()) == ""
