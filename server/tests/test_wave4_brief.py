"""Wave 4 (2026-10-18): the AI morning brief was rejected on every try.

The prompt always asked the model to cover steps and to reword every rule
action, while the validator rejects any mention of a metric held back from
the narrative (a running total of the day in progress, fix program A4). On a
day in progress steps and strain are always held back, and the red-recovery
rule's own text said "Keep strain low", so all three tries failed and the
template was served silently every morning. Synthetic values only.
"""
from __future__ import annotations

import logging

import pytest

from heliosd.narrative import brief, templates
from heliosd.narrative.validator import mention_pattern
from heliosd.store import db

from tests.test_wave1_r1 import DAY, SLEEP_SHORT, _aid, _rows, _signals

# A rule whose engine text names a held-back metric (the old red-recovery text).
STRAIN_RULE = {"key": "recovery_red", "category": "training",
               "text": "Recovery is in the red. Keep strain low today: mobility or an easy walk only."}

# A night that is in (recovery flagged), a short sleep, and two running totals
# of the day in progress.
ROWS = [("recovery_score", "flag", 21), ("sleep_duration", "flag", 4.5),
        ("steps", "in_progress", 1234), ("strain", "in_progress", 6.5)]


@pytest.fixture
def strain_rule(monkeypatch):
    monkeypatch.setattr(templates, "rule_based_actions",
                        lambda signals, flags: [dict(STRAIN_RULE), dict(SLEEP_SHORT)])


class _RecordingLM:
    primary = fallback = "fake-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def available(self):
        return True

    def structured(self, messages, schema, temperature=0.2, model=None):
        self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


GOOD = {"narrative": "A gentle day. Sleep ran short, so wind down earlier tonight.",
        "actions": [{"text": "Wind down earlier tonight.", "category": "sleep"}], "flags": []}


def test_the_red_recovery_rule_no_longer_names_strain():
    acts = templates.rule_based_actions([{"metric": "recovery_score", "state": "flag", "value": 20}], [])
    red = [a for a in acts if a["key"] == "recovery_red"]
    assert red and not __import__("re").search(mention_pattern("strain"), red[0]["text"], 2)


def test_split_rule_actions_keeps_a_rule_naming_a_held_back_metric_as_engine_text():
    speak, fixed = brief.split_rule_actions([STRAIN_RULE, SLEEP_SHORT], ["strain", "steps"])
    assert [a["key"] for a in speak] == ["sleep_short"]
    assert [a["key"] for a in fixed] == ["recovery_red"]
    assert brief.split_rule_actions([STRAIN_RULE], []) == ([STRAIN_RULE], [])


def test_avoid_sentence_names_plain_words_and_never_says_heart_rate_is_fine_when_rhr_is_blocked():
    assert brief.avoid_sentence([]) == ""
    one = brief.avoid_sentence(["heart_rate"])
    assert '"resting heart rate" and "heart rate variability" are fine' in one
    both = brief.avoid_sentence(["heart_rate", "resting_hr"])
    assert 'never write "heart rate" at all' in both and "are fine" not in both
    assert 'never write "HRV"' in brief.avoid_sentence(["hrv_rmssd"])


def test_the_prompt_asks_only_for_what_the_validator_allows(strain_rule):
    conn = db.connect_memory()
    _signals(conn, ROWS, [])
    lm = _RecordingLM([GOOD])
    out = brief.generate_brief(conn, lm, DAY, "Owner", force=True, allow_llm=True)
    prompt = lm.prompts[0]
    # steps is held back: the cover list no longer asks for it. hrv_rmssd and
    # resting_hr are awaited (no rows): they leave the cluster too.
    assert "sleep_duration, and steps" not in prompt
    assert "Cover, grouped as one story each and only for metrics present in signals: " \
           "recovery_score, sleep_duration." in prompt
    assert 'never use the word "strain"' in prompt and 'never use the words "step" or "steps"' in prompt
    assert 'never write "HRV"' in prompt and 'never write "heart rate"' not in prompt
    # The strain rule never reaches the model; the sleep rule does.
    data = prompt.split("DATA:\n", 1)[1]
    assert "Keep strain low" not in data and "Sleep ran short" in data
    assert out["validated"] is True and out["model"] == "fake-model"
    rows = _rows(conn)
    assert rows[_aid(STRAIN_RULE)]["text"] == STRAIN_RULE["text"]
    assert rows[_aid(STRAIN_RULE)]["created_by"] == "engine"
    assert rows[_aid(SLEEP_SHORT)]["text"] == "Wind down earlier tonight."
    assert rows[_aid(SLEEP_SHORT)]["created_by"] == "llm"
    # The rule order is kept.
    assert [a["action_id"] for a in out["actions"]] == [_aid(STRAIN_RULE), _aid(SLEEP_SHORT)]


def test_a_rejected_try_is_retried_with_a_plain_instruction_and_can_still_validate(strain_rule):
    conn = db.connect_memory()
    _signals(conn, ROWS, [])
    bad = dict(GOOD, narrative="Recovery is low and strain is building.")
    lm = _RecordingLM([bad, GOOD])
    out = brief.generate_brief(conn, lm, DAY, "Owner", force=True, allow_llm=True)
    assert out["validated"] is True
    assert len(lm.prompts) == 2
    assert 'remove "strain"' in lm.prompts[1]


def test_retries_running_out_are_logged_without_numbers(strain_rule, caplog):
    conn = db.connect_memory()
    _signals(conn, ROWS, [])
    bad = dict(GOOD, narrative="Strain is 77 already and steps are 1234.")
    lm = _RecordingLM([bad])
    with caplog.at_level(logging.WARNING, logger="heliosd"):
        out = brief.generate_brief(conn, lm, DAY, "Owner", force=True, allow_llm=True)
    assert out["validated"] is False and out["model"] == brief.FINAL_TEMPLATE
    assert len(lm.prompts) == 3
    msgs = [r.getMessage() for r in caplog.records if "failed validation on every try" in r.getMessage()]
    assert len(msgs) == 1, caplog.text
    assert "mentions steps" in msgs[0] and "mentions strain" in msgs[0]
    assert "77" not in msgs[0] and "1234" not in msgs[0]


def test_a_crash_after_a_rejected_try_logs_the_crash_only(strain_rule, caplog):
    conn = db.connect_memory()
    _signals(conn, ROWS, [])

    class _RejectThenBoom(_RecordingLM):
        def structured(self, *a, **k):
            if not self.prompts:
                self.prompts.append("x")
                return dict(GOOD, narrative="Strain is building.")
            raise TimeoutError("synthetic")

    with caplog.at_level(logging.WARNING, logger="heliosd"):
        out = brief.generate_brief(conn, _RejectThenBoom([{}]), DAY, "Owner", force=True, allow_llm=True)
    assert out["validated"] is False and out["model"] == brief.FINAL_TEMPLATE
    assert "model call failed (TimeoutError)" in caplog.text
    assert "failed validation on every try" not in caplog.text
