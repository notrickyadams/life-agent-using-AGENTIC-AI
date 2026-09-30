"""Tests for src/agent/synthesis.py - the one judgement step.

No model is called. The important tests are the guardrail (does a summary
that drops your overdue work get caught?) and the cost rule (can a second
paid call happen in one run?).
"""

from datetime import date

import pytest

from src import config
from src.agent import synthesis
from src.agent.synthesis import SynthesisError, check_covers_overdue, synthesise
from src.db import queries
from src.rhythm.config import Day, Rhythm
from src.rhythm.today import build_plan

WEDNESDAY = date(2026, 9, 30)


def _rhythm() -> Rhythm:
    days = {d: Day(name=d) for d in
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
    return Rhythm(timezone="Africa/Cairo", due_soon_days=3, show_overdue=True, days=days)


def _add(db, event_id, **fields):
    queries.upsert_events(db, [{
        "id": event_id,
        "source": "classroom" if event_id.startswith("classroom") else "calendar",
        "type": "assignment" if event_id.startswith("classroom") else "event",
        "title": fields.pop("title", event_id),
        "raw_json": "{}",
        **fields,
    }])


def _plan_with_overdue(db):
    _add(db, "classroom:late", title="Lab report 3", due_at="2026-09-28T20:59:00Z",
         submission_state="CREATED", course_name="Systems")
    return build_plan(db, _rhythm(), WEDNESDAY)


def _busy_plan(db):
    _add(db, "calendar:lecture", title="Networks lecture",
         due_at="2026-09-30T06:30:00Z", end_at="2026-09-30T08:00:00Z")
    return build_plan(db, _rhythm(), WEDNESDAY)


# --- the guardrail: the one omission we have actually seen -----------------

def test_a_summary_that_names_the_overdue_item_passes(db):
    plan = _plan_with_overdue(db)

    complete, note = check_covers_overdue("Start with the Lab report for Systems.", plan)

    assert complete is True
    assert note == ""


def test_saying_overdue_at_all_passes(db):
    plan = _plan_with_overdue(db)

    assert check_covers_overdue("Deal with the overdue work first.", plan)[0] is True
    assert check_covers_overdue("You have something late.", plan)[0] is True


def test_a_summary_that_drops_the_overdue_item_is_flagged(db):
    """Observed with llama3.1:8b: a fluent summary that never mentioned it.
    Nothing in the text signals the omission, which is the whole problem."""
    plan = _plan_with_overdue(db)

    complete, note = check_covers_overdue(
        "Start with two hours of study, then attend your lecture at 09:30.", plan
    )

    assert complete is False
    assert "Lab report 3" in note


def test_nothing_overdue_means_nothing_to_check(db):
    plan = _busy_plan(db)

    assert check_covers_overdue("Anything at all.", plan) == (True, "")


def test_the_check_is_case_insensitive(db):
    plan = _plan_with_overdue(db)

    assert check_covers_overdue("LAB REPORT is late", plan)[0] is True


def test_short_words_do_not_count_as_a_mention(db):
    """Matching on 'the' or 'for' would make the check pass on anything."""
    _add(db, "classroom:late", title="Do the lab", due_at="2026-09-28T20:59:00Z",
         submission_state="CREATED")
    plan = build_plan(db, _rhythm(), WEDNESDAY)

    assert check_covers_overdue("Go for a walk and think about the day.", plan)[0] is False


# --- choosing a backend -----------------------------------------------------

def test_none_means_no_summary_and_no_call(db):
    summary = synthesise(_busy_plan(db), backend="none")

    assert summary.text == ""
    assert summary.usable is False
    assert summary.claude_calls == 0


def test_an_empty_plan_is_never_sent_to_a_model(db, monkeypatch):
    """No model should be asked, or paid, to write about an empty list."""
    def must_not_run(*a, **k):
        raise AssertionError("a model was called for an empty plan")

    monkeypatch.setattr(synthesis, "_synthesise_with_ollama", must_not_run)
    monkeypatch.setattr(synthesis, "_synthesise_with_claude", must_not_run)

    summary = synthesise(build_plan(db, _rhythm(), WEDNESDAY), backend="ollama")

    assert summary.text == ""
    assert summary.note == "nothing needs prioritising today"


def test_an_unknown_backend_is_rejected(db):
    with pytest.raises(SynthesisError, match="expected"):
        synthesise(_busy_plan(db), backend="gpt-9")


def test_the_backend_defaults_to_the_setting(db, monkeypatch):
    monkeypatch.setattr(config, "SYNTHESIS_MODEL", "none")

    assert synthesise(_busy_plan(db)).backend == "none"


# --- ollama backend ---------------------------------------------------------

def test_ollama_backend_returns_its_text(db, monkeypatch):
    monkeypatch.setattr(synthesis.config, "SYNTHESIS_MODEL", "ollama")
    monkeypatch.setattr("src.agent.local_model.is_available", lambda: True)
    monkeypatch.setattr("src.agent.local_model.generate", lambda *a, **k: "  Do the lab first.  ")

    summary = synthesise(_plan_with_overdue(db), backend="ollama")

    assert summary.text == "Do the lab first."
    assert summary.backend == "ollama"
    assert summary.claude_calls == 0          # free, always


def test_ollama_being_down_is_reported_not_silent(db, monkeypatch):
    monkeypatch.setattr("src.agent.local_model.is_available", lambda: False)

    with pytest.raises(SynthesisError, match="Ollama is not running"):
        synthesise(_busy_plan(db), backend="ollama")


def test_a_summary_that_fails_the_check_is_discarded_not_captioned(db, monkeypatch):
    """A confident false sentence at the top of the page is worse than
    silence - it sends you to do the wrong thing, with a reason attached,
    and a footnote underneath does not undo that. The sections below stay
    correct, so nothing is lost but a paragraph that was wrong."""
    monkeypatch.setattr("src.agent.local_model.is_available", lambda: True)
    monkeypatch.setattr("src.agent.local_model.generate",
                        lambda *a, **k: "Have a productive day studying.")

    summary = synthesise(_plan_with_overdue(db), backend="ollama")

    assert summary.usable is False          # nothing reaches the page
    assert summary.text == ""
    assert summary.complete is False
    assert "Lab report 3" in summary.note   # the log still says why


def test_an_invented_overdue_claim_is_also_discarded(db, monkeypatch):
    monkeypatch.setattr("src.agent.local_model.is_available", lambda: True)
    monkeypatch.setattr("src.agent.local_model.generate",
                        lambda *a, **k: "The networks lecture is overdue.")

    summary = synthesise(_busy_plan(db), backend="ollama")

    assert summary.usable is False
    assert "calls something overdue when nothing is" in summary.note


def test_a_good_summary_is_returned_untouched(db, monkeypatch):
    monkeypatch.setattr("src.agent.local_model.is_available", lambda: True)
    monkeypatch.setattr("src.agent.local_model.generate",
                        lambda *a, **k: "Start with the overdue Lab report 3.")

    summary = synthesise(_plan_with_overdue(db), backend="ollama")

    assert summary.usable is True
    assert summary.complete is True
    assert summary.note == ""


# --- claude backend and the cost rule --------------------------------------

def test_claude_without_a_key_is_refused_before_any_request(db, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")

    with pytest.raises(SynthesisError, match="ANTHROPIC_API_KEY"):
        synthesise(_busy_plan(db), backend="claude")


def test_a_claude_call_is_recorded_against_the_run(db, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-fake")
    run_id = queries.start_run(db)

    class FakeBlock:
        type = "text"
        text = "Do the lab first, it is overdue."

    class FakeMessages:
        def create(self, **kwargs):
            class R:
                content = [FakeBlock()]
            return R()

    class FakeAnthropic:
        def __init__(self, api_key=None):
            self.messages = FakeMessages()

    import sys, types
    module = types.ModuleType("anthropic")
    module.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)

    summary = synthesise(_plan_with_overdue(db), backend="claude", conn=db, run_id=run_id)

    assert summary.claude_calls == 1
    assert queries.count_claude_calls(db, run_id) == 1
    assert "overdue" in summary.text


def test_a_second_claude_call_today_is_refused(db, monkeypatch):
    """The cost rule lives in the database, not in a promise. Refused as a
    SynthesisError - the summary is skipped, which is the rule working, not
    the run breaking."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-fake")
    run_id = queries.start_run(db)
    queries.record_claude_call(db, run_id, timezone_name=config.SYNTHESIS_TIMEZONE)  # the day's one call

    import sys, types
    module = types.ModuleType("anthropic")

    def must_not_run(*a, **k):
        raise AssertionError("a second paid call was made today")

    module.Anthropic = must_not_run
    monkeypatch.setitem(sys.modules, "anthropic", module)

    with pytest.raises(SynthesisError, match="already been used"):
        synthesise(_plan_with_overdue(db), backend="claude", conn=db, run_id=run_id)


def test_a_claude_call_that_cannot_be_counted_is_refused(db, monkeypatch):
    """With no database there is no way to enforce one-a-day, so the call
    is refused rather than made uncounted. A cost limit must not fail open."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-fake")

    import sys, types
    module = types.ModuleType("anthropic")

    def must_not_run(*a, **k):
        raise AssertionError("an uncounted paid call was made")

    module.Anthropic = must_not_run
    monkeypatch.setitem(sys.modules, "anthropic", module)

    with pytest.raises(SynthesisError, match="counted"):
        synthesise(_plan_with_overdue(db), backend="claude")


def test_the_call_is_recorded_even_if_the_request_then_fails(db, monkeypatch):
    """A failed call still cost money and still used the day's budget."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-fake")
    run_id = queries.start_run(db)

    import sys, types
    module = types.ModuleType("anthropic")

    class Exploding:
        def __init__(self, api_key=None):
            class M:
                def create(self, **kwargs):
                    raise RuntimeError("503 from the API")
            self.messages = M()

    module.Anthropic = Exploding
    monkeypatch.setitem(sys.modules, "anthropic", module)

    with pytest.raises(SynthesisError, match="failed"):
        synthesise(_plan_with_overdue(db), backend="claude", conn=db, run_id=run_id)

    assert queries.count_claude_calls(db, run_id) == 1


# --- the prompt -------------------------------------------------------------

def test_the_prompt_states_the_date_and_the_plan(db):
    prompt = synthesis.build_prompt(_plan_with_overdue(db))

    assert "Wednesday 30 September 2026" in prompt
    assert "Lab report 3" in prompt
    assert "OVERDUE" in prompt


def test_the_system_prompt_defines_every_section(db):
    for section in ("OVERDUE", "TODAY", "DUE SOON", "LATER"):
        assert section in synthesis.SYSTEM_PROMPT


def test_the_system_prompt_forbids_inventing_things():
    assert "Invent nothing" in synthesis.SYSTEM_PROMPT


# --- nothing to prioritise --------------------------------------------------

def test_a_day_with_only_distant_work_gets_no_summary(db, monkeypatch):
    """The summary answers "what first". With nothing urgent there is no
    first, and a model given nothing to rank invents something to rank:
    llama3.1:8b replied "start with the DUE SOON section" when no such
    section was on the page."""
    def must_not_run(*a, **k):
        raise AssertionError("a model was asked to rank nothing")

    monkeypatch.setattr(synthesis, "_synthesise_with_ollama", must_not_run)
    monkeypatch.setattr(synthesis, "_synthesise_with_claude", must_not_run)

    _add(db, "classroom:far", title="Assignment 1", due_at="2026-11-30T20:59:00Z",
         submission_state="CREATED")
    plan = build_plan(db, _rhythm(), WEDNESDAY)

    summary = synthesise(plan, backend="ollama")

    assert plan.later                      # there IS work listed
    assert summary.usable is False
    assert summary.note == "nothing needs prioritising today"


def test_anything_urgent_does_get_a_summary(db, monkeypatch):
    monkeypatch.setattr("src.agent.local_model.is_available", lambda: True)
    monkeypatch.setattr("src.agent.local_model.generate", lambda *a, **k: "Do the lab, it is overdue.")

    summary = synthesise(_plan_with_overdue(db), backend="ollama")

    assert summary.usable is True


def test_the_system_prompt_forbids_naming_absent_sections():
    assert "not present above" in synthesis.SYSTEM_PROMPT


def test_a_summary_that_invents_overdue_work_is_flagged(db):
    """Observed on the real page: with nothing overdue, llama3.1:8b wrote
    "the Systems assignment is overdue" about work due nine days later.
    Inventing urgency sends you to do the wrong thing first."""
    plan = _busy_plan(db)
    assert plan.overdue == []

    complete, note = check_covers_overdue(
        "Do the lecture first. The Systems assignment is overdue.", plan
    )

    assert complete is False
    assert "calls something overdue when nothing is" in note


def test_a_summary_with_nothing_overdue_and_no_such_claim_passes(db):
    plan = _busy_plan(db)

    assert check_covers_overdue("Start with the 09:30 lecture.", plan) == (True, "")


def test_the_word_late_alone_does_not_trip_the_check(db):
    """"running late" or "later today" must not read as an overdue claim."""
    plan = _busy_plan(db)

    assert check_covers_overdue("You can do this later today.", plan)[0] is True


def test_later_is_not_read_as_late(db):
    """Regression: "th-IS LATE-r today" matched the substring "is late", so
    an ordinary sentence was read as claiming something was overdue."""
    plan = _busy_plan(db)

    for phrase in ["You can do this later today.",
                   "Do it later.",
                   "Deal with this in the latest session."]:
        assert check_covers_overdue(phrase, plan) == (True, ""), phrase
