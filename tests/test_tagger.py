"""Tests for src/agent/tagger.py - classifying items with the local model.

No Ollama needed. The parsing tests are the important ones: they are what
guarantees a model that rambles, invents a label, or answers with a sentence
produces NO tag rather than a wrong one.
"""

from datetime import date

import pytest

from src.agent import tagger
from src.agent.tagger import ALLOWED_TAGS, MODEL_TAG_SOURCE, parse_tag
from src.db import queries
from src.rhythm.today import Item


def make_item(item_id="classroom:c/1", title="Assignment 1", course=None, description=""):
    return Item(
        id=item_id, source="classroom", type="assignment", title=title,
        course_name=course, submission_state="CREATED", link=None,
        all_day=False, due_at="2026-10-08T20:59:00Z",
        local_date=date(2026, 10, 8), local_time="23:59", description=description,
    )


# --- parsing: the guarantee that a wrong tag is never stored ---------------

@pytest.mark.parametrize("tag", sorted(ALLOWED_TAGS))
def test_every_allowed_tag_is_accepted_on_its_own(tag):
    assert parse_tag(tag) == tag


@pytest.mark.parametrize("answer,expected", [
    ("exam", "exam"),
    ("  exam  ", "exam"),
    ("Exam", "exam"),
    ("EXAM", "exam"),
    ("exam.", "exam"),
    ("**exam**", "exam"),
    ("`lab`", "lab"),
    ("Answer: quiz", "quiz"),
    ("The answer is lab.", "lab"),
])
def test_real_model_output_shapes_are_handled(answer, expected):
    """Small models pad their answers; that should not cost a valid tag."""
    assert parse_tag(answer) == expected


@pytest.mark.parametrize("answer", [
    "",
    "   ",
    "none",
    "None.",
    "urgent-ish",
    "homework",                       # plausible, but not in the vocabulary
    "I am not sure what this is",
    "Assignment 1 for Computer Networks",
])
def test_anything_unrecognised_yields_no_tag(answer):
    assert parse_tag(answer) is None


def test_an_answer_naming_two_tags_is_refused():
    """Ambiguity is not resolved by guessing - the first word might be wrong."""
    assert parse_tag("this is a lab, not an exam") is None
    assert parse_tag("exam or quiz") is None


def test_a_tag_inside_a_longer_word_does_not_count():
    """'examination' and 'labour' must not be read as 'exam' and 'lab'."""
    assert parse_tag("examination") is None
    assert parse_tag("labouring over it") is None


def test_a_very_long_answer_is_cut_off_before_parsing():
    """A model that ignores instructions must not cost unbounded work."""
    answer = "lab " + ("x" * 10_000) + " exam"

    # Only the first 200 characters are read, so the trailing 'exam' is
    # never seen and the answer stays unambiguous.
    assert parse_tag(answer) == "lab"


def test_none_is_a_valid_answer_meaning_no_tag():
    assert parse_tag("none") is None


# --- the prompt -------------------------------------------------------------

def test_the_prompt_lists_every_allowed_tag():
    prompt = tagger.build_prompt(make_item())

    for tag in ALLOWED_TAGS:
        assert tag in prompt
    assert "none" in prompt          # an explicit way out


def test_the_prompt_includes_the_description():
    """The description is the part that distinguishes 'Assignment 1' from
    'Assignment 1' - the whole reason a model is involved."""
    prompt = tagger.build_prompt(
        make_item(description="Submit your lab report for experiment 2")
    )

    assert "lab report for experiment 2" in prompt


def test_the_prompt_survives_an_item_with_no_description():
    prompt = tagger.build_prompt(make_item())

    assert "Assignment 1" in prompt
    assert "Description:" not in prompt


def test_the_course_name_is_included_when_there_is_one():
    prompt = tagger.build_prompt(make_item(course="Computer networks C1 26-27"))

    assert "Computer networks C1 26-27" in prompt


# --- classify ---------------------------------------------------------------

def test_classify_returns_the_parsed_tag(monkeypatch):
    monkeypatch.setattr(tagger.local_model, "generate", lambda *a, **k: "lab")

    assert tagger.classify(make_item()) == "lab"


def test_classify_asks_for_a_short_deterministic_answer(monkeypatch):
    captured = {}

    def fake_generate(prompt, **kwargs):
        captured.update(kwargs)
        return "exam"

    monkeypatch.setattr(tagger.local_model, "generate", fake_generate)
    tagger.classify(make_item())

    assert captured["temperature"] == 0.0     # same item, same answer tomorrow
    assert captured["max_tokens"] <= 10       # a correct answer is one word
    assert "one word" in captured["system"]


def test_classify_never_raises_when_the_model_is_down(monkeypatch):
    """One unclassifiable item must not stop the rest, or the run."""
    def boom(*a, **k):
        raise tagger.local_model.LocalModelError("ollama is not running")

    monkeypatch.setattr(tagger.local_model, "generate", boom)

    assert tagger.classify(make_item()) is None


def test_classify_items_skips_the_ones_with_no_tag(monkeypatch):
    answers = {"classroom:c/1": "lab", "classroom:c/2": "nonsense", "classroom:c/3": "exam"}

    def fake_generate(prompt, **k):
        for item_id, answer in answers.items():
            if item_id.split("/")[-1] in prompt:
                return answer
        return ""

    monkeypatch.setattr(tagger.local_model, "generate", fake_generate)

    items = [make_item("classroom:c/1", title="1"),
             make_item("classroom:c/2", title="2"),
             make_item("classroom:c/3", title="3")]
    tags, asked = tagger.classify_items(items)

    assert tags == {"classroom:c/1": "lab", "classroom:c/3": "exam"}
    # All three WERE asked - the middle one simply gave an unusable answer.
    # That distinction is what stops an outage wiping everything.
    assert asked == {"classroom:c/1", "classroom:c/2", "classroom:c/3"}


# --- storage ----------------------------------------------------------------

def _add_event(db, event_id):
    queries.upsert_events(db, [{
        "id": event_id, "source": "classroom", "type": "assignment",
        "title": event_id, "raw_json": "{}",
    }])


def test_tags_are_stored_under_the_model_source(db):
    _add_event(db, "classroom:c/1")

    tagger.store_tags(db, {"classroom:c/1": "lab"}, {"classroom:c/1"})

    assert queries.get_tags(db, "classroom:c/1", source=MODEL_TAG_SOURCE) == ["lab"]


def test_model_tags_do_not_disturb_rule_tags(db):
    """The two sources coexist; neither overwrites the other."""
    _add_event(db, "classroom:c/1")
    queries.set_tags(db, "classroom:c/1", ["exam"], "rule")

    tagger.store_tags(db, {"classroom:c/1": "lab"}, {"classroom:c/1"})

    assert queries.get_tags(db, "classroom:c/1") == ["exam", "lab"]
    assert queries.get_tags(db, "classroom:c/1", source="rule") == ["exam"]


def test_a_tag_that_no_longer_applies_is_removed(db):
    _add_event(db, "classroom:c/1")
    tagger.store_tags(db, {"classroom:c/1": "lab"}, {"classroom:c/1"})

    # The next run classifies it differently.
    tagger.store_tags(db, {"classroom:c/1": "exam"}, {"classroom:c/1"})

    assert queries.get_tags(db, "classroom:c/1", source=MODEL_TAG_SOURCE) == ["exam"]


def test_an_item_asked_about_and_given_no_tag_loses_its_old_one(db):
    """That answer is current, so the old tag is out of date."""
    _add_event(db, "classroom:c/1")
    tagger.store_tags(db, {"classroom:c/1": "lab"}, {"classroom:c/1"})

    tagger.store_tags(db, {}, {"classroom:c/1"})      # asked, no tag

    assert queries.get_tags(db, "classroom:c/1", source=MODEL_TAG_SOURCE) == []


def test_an_item_that_could_not_be_asked_keeps_its_tag(db):
    """The fix for an Ollama outage wiping every tag."""
    _add_event(db, "classroom:c/1")
    tagger.store_tags(db, {"classroom:c/1": "lab"}, {"classroom:c/1"})

    tagger.store_tags(db, {}, set())                  # nothing was asked

    assert queries.get_tags(db, "classroom:c/1", source=MODEL_TAG_SOURCE) == ["lab"]


def test_an_outage_leaves_every_tag_intact(db, monkeypatch):
    """End to end: the model is down, so nothing is asked, so nothing changes."""
    _add_event(db, "classroom:c/1")
    _add_event(db, "classroom:c/2")
    tagger.store_tags(db, {"classroom:c/1": "lab", "classroom:c/2": "exam"},
                      {"classroom:c/1", "classroom:c/2"})

    def down(*a, **k):
        raise tagger.local_model.LocalModelError("ollama is not running")

    monkeypatch.setattr(tagger.local_model, "generate", down)

    tags, asked = tagger.classify_items([make_item("classroom:c/1"), make_item("classroom:c/2")])
    tagger.store_tags(db, tags, asked)

    assert (tags, asked) == ({}, set())
    assert queries.get_tags(db, "classroom:c/1", source=MODEL_TAG_SOURCE) == ["lab"]
    assert queries.get_tags(db, "classroom:c/2", source=MODEL_TAG_SOURCE) == ["exam"]


def test_a_partial_outage_only_touches_what_was_asked(db, monkeypatch):
    _add_event(db, "classroom:c/1")
    _add_event(db, "classroom:c/2")
    tagger.store_tags(db, {"classroom:c/1": "lab", "classroom:c/2": "exam"},
                      {"classroom:c/1", "classroom:c/2"})

    def flaky(prompt, **k):
        if "classroom:c/2" in prompt or "two" in prompt:
            raise tagger.local_model.LocalModelError("timed out")
        return "quiz"

    monkeypatch.setattr(tagger.local_model, "generate", flaky)

    items = [make_item("classroom:c/1", title="classroom:c/1"),
             make_item("classroom:c/2", title="classroom:c/2")]
    tags, asked = tagger.classify_items(items)
    tagger.store_tags(db, tags, asked)

    assert queries.get_tags(db, "classroom:c/1", source=MODEL_TAG_SOURCE) == ["quiz"]
    assert queries.get_tags(db, "classroom:c/2", source=MODEL_TAG_SOURCE) == ["exam"]


# --- negated answers --------------------------------------------------------

@pytest.mark.parametrize("answer", [
    "not a lab",
    "No exam",
    "none - not quiz",
    "isn't a project",
    "never a reading",
])
def test_a_negated_answer_yields_no_tag(answer):
    """'not a lab' names a tag and means the opposite. Reading the tag out
    of it would be exactly wrong - worse than answering nothing."""
    assert parse_tag(answer) is None


def test_a_plain_answer_still_works_after_the_negation_guard():
    assert parse_tag("lab") == "lab"
    assert parse_tag("Answer: quiz") == "quiz"
