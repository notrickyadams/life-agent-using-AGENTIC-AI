"""Tests for src/notion/writer.py - connection checks and the overwrite guard.

No network: a fake Notion client stands in for the real one. The guard is
the important part - it is what stops a mistyped page id from quietly
emptying a page you actually use.
"""

from datetime import date

import pytest

from src import config
from src.db import queries
from src.notion import writer
from src.notion.writer import MARKER_TEXT, NotionError
from src.rhythm.config import Day, Rhythm
from src.rhythm.today import build_plan


def _text_block(kind: str, text: str) -> dict:
    return {"type": kind, kind: {"rich_text": [{"plain_text": text}]}}


def _marker_block() -> dict:
    return _text_block("callout", MARKER_TEXT)


def _page(title="Life Agent", archived=False, in_trash=False) -> dict:
    return {
        "object": "page",
        "id": "11111111-2222-3333-4444-555555555555",
        "archived": archived,
        "in_trash": in_trash,
        "url": "https://notion.so/example",
        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
    }


class FakeNotion:
    """Mimics the parts of notion_client.Client that the writer uses."""

    def __init__(self, page=None, blocks=None, page_error=None, pages_of_blocks=None):
        self._page = page if page is not None else _page()
        self._pages_of_blocks = pages_of_blocks or [blocks or []]
        self._page_error = page_error
        outer = self

        class _Pages:
            def retrieve(self, page_id):
                if outer._page_error:
                    raise outer._page_error
                return outer._page

        class _Children:
            def list(self, block_id, start_cursor=None):
                index = 0 if start_cursor is None else int(start_cursor)
                is_last = index >= len(outer._pages_of_blocks) - 1
                return {
                    "results": outer._pages_of_blocks[index],
                    "has_more": not is_last,
                    "next_cursor": None if is_last else str(index + 1),
                }

        class _Blocks:
            children = _Children()

        class _Users:
            def me(self):
                return {
                    "name": "Life Agent",
                    "type": "bot",
                    "bot": {"workspace_name": "Test WS", "owner": {"type": "workspace"}},
                }

        self.pages, self.blocks, self.users = _Pages(), _Blocks(), _Users()


def _api_error(code: str):
    """Build a real APIResponseError, with the real constructor signature.

    Using the real exception type matters: the writer branches on exc.code,
    and a hand-rolled stand-in would not prove that branch works.
    """
    import httpx
    from notion_client.errors import APIResponseError

    return APIResponseError(
        code=code,
        status=404,
        message=code,
        headers=httpx.Headers({}),
        raw_body_text=code,
    )


@pytest.fixture(autouse=True)
def notion_settings(monkeypatch):
    monkeypatch.setattr(config, "NOTION_API_KEY", "ntn_fake")
    monkeypatch.setattr(config, "NOTION_PARENT_ID", "11111111222233334444555555555555")


# --- the overwrite guard ----------------------------------------------------

def test_an_empty_page_is_safe_to_write():
    summary = writer.check_page(FakeNotion(blocks=[]))

    assert summary["safe_to_write"] is True
    assert summary["ownership"] == "empty"
    assert summary["blocks"] == 0


def _owned(block_id: str, kind="paragraph", text="agent content") -> dict:
    block = _text_block(kind, text)
    block["id"] = block_id
    return block


def test_a_page_the_agent_has_written_to_before_is_safe():
    blocks = [_owned("b1", "callout", MARKER_TEXT), _owned("b2", "heading_1", "Wednesday")]

    summary = writer.check_page(FakeNotion(blocks=blocks), known_block_ids=["b1", "b2"])

    assert summary["ownership"] == "agent"
    assert summary["agent_blocks"] == 2
    assert summary["other_blocks"] == 0


def test_a_page_with_content_the_agent_never_wrote_is_refused():
    """The whole point of the guard: never destroy something you typed."""
    blocks = [_text_block("paragraph", "My thesis notes, please do not delete")]

    with pytest.raises(NotionError) as excinfo:
        writer.check_page(FakeNotion(blocks=blocks), known_block_ids=[])

    message = str(excinfo.value)
    assert "no record of writing" in message
    assert "NOTION_PARENT_ID" in message      # tells you how to fix it
    assert "thesis notes" in message          # shows what it found


def test_your_own_blocks_alongside_the_agents_do_not_block_a_write():
    """Regression: a marker-position rule locked the agent out of its own page.

    Appends land at the END of a page, so after one write-then-delete cycle
    anything you added sits ABOVE the marker. Requiring the marker to be
    first made the agent refuse to ever write again.
    """
    blocks = [
        _owned("yours", "paragraph", "MY OWN NOTE"),      # not in known ids
        _owned("b1", "callout", MARKER_TEXT),
        _owned("b2", "heading_1", "Thursday"),
    ]

    summary = writer.check_page(FakeNotion(blocks=blocks), known_block_ids=["b1", "b2"])

    assert summary["ownership"] == "agent"
    assert summary["agent_blocks"] == 2
    assert summary["other_blocks"] == 1


def test_a_page_you_cleared_by_hand_is_treated_as_fresh():
    """You deleted the agent's blocks yourself; its record is simply stale."""
    summary = writer.check_page(FakeNotion(blocks=[]), known_block_ids=["gone-1", "gone-2"])

    assert summary["ownership"] == "cleared"
    assert summary["safe_to_write"] is True


# --- page problems ----------------------------------------------------------

def test_a_page_the_integration_cannot_see_explains_the_connection_step():
    client = FakeNotion(page_error=_api_error("object_not_found"))

    with pytest.raises(NotionError) as excinfo:
        writer.check_page(client)

    message = str(excinfo.value)
    assert "Connections" in message      # the step everyone misses
    assert "integration" in message


def test_a_trashed_page_is_refused():
    with pytest.raises(NotionError, match="trash"):
        writer.check_page(FakeNotion(page=_page(in_trash=True)))


def test_an_archived_page_is_refused():
    with pytest.raises(NotionError, match="trash"):
        writer.check_page(FakeNotion(page=_page(archived=True)))


def test_missing_page_id_is_reported(monkeypatch):
    monkeypatch.setattr(config, "NOTION_PARENT_ID", "")

    with pytest.raises(NotionError, match="NOTION_PARENT_ID"):
        writer.get_page(FakeNotion())


def test_missing_token_is_reported(monkeypatch):
    monkeypatch.setattr(config, "NOTION_API_KEY", "")

    with pytest.raises(NotionError, match="NOTION_API_KEY"):
        writer.get_client()


# --- details ----------------------------------------------------------------

def test_blocks_are_read_across_pagination():
    client = FakeNotion(
        pages_of_blocks=[[_marker_block()], [_text_block("paragraph", "second page")]]
    )

    assert len(writer.list_blocks(client, "id")) == 2


def test_page_title_is_found_by_property_type_not_name():
    """A database page's title property is often called something else."""
    page = {"properties": {"Name": {"type": "title", "title": [{"plain_text": "My Page"}]}}}

    assert writer.page_title(page) == "My Page"


def test_a_page_with_no_title_is_described_not_crashed():
    assert writer.page_title({"properties": {}}) == "(untitled)"


def test_connection_description_reports_the_integration():
    who = writer.describe_connection(FakeNotion())

    assert who["name"] == "Life Agent"
    assert who["workspace"] == "Test WS"


def test_notion_configured_needs_both_settings(monkeypatch):
    assert config.notion_configured() is True

    monkeypatch.setattr(config, "NOTION_PARENT_ID", "")
    assert config.notion_configured() is False


# ============================================================================
# Rendering a DayPlan into Notion blocks. Pure - no client involved at all.
# ============================================================================

MONDAY = date(2026, 9, 21)
TUESDAY = date(2026, 9, 22)


def _rhythm(**overrides) -> Rhythm:
    """A rhythm with a themed Monday carrying a focus line."""
    days = {
        name: Day(name=name)
        for name in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    }
    days["monday"] = Day(
        name="monday", theme="certification_study",
        focus="Two hours of study", keywords=("exam",),
    )
    defaults = dict(timezone="Africa/Cairo", due_soon_days=3, show_overdue=True, days=days)
    return Rhythm(**{**defaults, **overrides})


def _add(db, event_id, **fields):
    queries.upsert_events(db, [{
        "id": event_id,
        "source": "classroom" if event_id.startswith("classroom") else "calendar",
        "type": "assignment" if event_id.startswith("classroom") else "event",
        "title": fields.pop("title", event_id),
        "raw_json": "{}",
        **fields,
    }])


def _text_of(block) -> str:
    kind = block["type"]
    return "".join(part["text"]["content"] for part in block[kind].get("rich_text", []))


def _texts(blocks) -> list[str]:
    return [_text_of(b) for b in blocks]


def _kinds(blocks) -> list[str]:
    return [b["type"] for b in blocks]


# --- structure --------------------------------------------------------------

def test_the_page_carries_the_agent_marker(db):
    """A label saying what the page is. Its POSITION is deliberately not
    load-bearing - warnings sit above it, and safety comes from recorded
    block ids, not from what happens to be first."""
    blocks = writer.render_plan(build_plan(db, _rhythm(), MONDAY))

    assert any(writer.is_agent_marker(b) for b in blocks)


def test_the_heading_names_the_day_and_theme(db):
    blocks = writer.render_plan(build_plan(db, _rhythm(), MONDAY))

    heading = next(b for b in blocks if b["type"] == "heading_1")
    assert "Monday 21 September 2026" in _text_of(heading)
    assert "certification_study" in _text_of(heading)


def test_a_themeless_day_gets_a_plain_heading(db):
    blocks = writer.render_plan(build_plan(db, _rhythm(), TUESDAY))

    heading = next(b for b in blocks if b["type"] == "heading_1")
    assert _text_of(heading) == "Tuesday 22 September 2026"


def test_the_page_states_how_old_the_data_is(db):
    """The page must never silently imply the data is current."""
    blocks = writer.render_plan(build_plan(db, _rhythm(), MONDAY))

    assert any("Google data:" in t for t in _texts(blocks))


def test_the_focus_line_appears_as_a_quote(db):
    blocks = writer.render_plan(build_plan(db, _rhythm(), MONDAY))

    quotes = [b for b in blocks if b["type"] == "quote"]
    assert _texts(quotes) == ["Two hours of study"]


def test_no_focus_means_no_quote_block(db):
    days = {**_rhythm().days, "monday": Day(name="monday", theme="study")}
    blocks = writer.render_plan(build_plan(db, _rhythm(days=days), MONDAY))

    assert "quote" not in _kinds(blocks)


# --- sections ---------------------------------------------------------------

def test_sections_appear_in_priority_order(db):
    _add(db, "classroom:c/late", title="Late", due_at="2026-09-18T20:59:00Z",
         submission_state="CREATED")
    _add(db, "calendar:now", title="Today thing", due_at="2026-09-21T06:00:00Z")
    _add(db, "classroom:c/soon", title="Soon", due_at="2026-09-23T20:59:00Z",
         submission_state="NEW")
    _add(db, "classroom:c/far", title="Final Exam", due_at="2026-10-30T20:59:00Z",
         submission_state="CREATED")

    texts = _texts(writer.render_plan(build_plan(db, _rhythm(), MONDAY)))
    headings = [t for t in texts if t.startswith(("Overdue", "Today", "Due soon", "Fits"))]

    assert headings[0].startswith("Overdue")
    assert headings[1].startswith("Today")
    assert headings[2].startswith("Due soon")
    assert headings[3].startswith("Fits")
    assert "certification_study" in headings[3]


def test_empty_sections_are_left_out(db):
    _add(db, "calendar:now", title="Only thing", due_at="2026-09-21T06:00:00Z")

    texts = _texts(writer.render_plan(build_plan(db, _rhythm(), MONDAY)))

    assert any(t.startswith("Today (1)") for t in texts)
    assert not any(t.startswith("Overdue") for t in texts)
    assert not any(t.startswith("Due soon") for t in texts)


def test_headings_count_their_items(db):
    _add(db, "calendar:a", title="A", due_at="2026-09-21T06:00:00Z")
    _add(db, "calendar:b", title="B", due_at="2026-09-21T08:00:00Z")

    assert "Today (2)" in _texts(writer.render_plan(build_plan(db, _rhythm(), MONDAY)))


def test_items_outside_today_carry_their_date(db):
    """A bare time is ambiguous when the item is not today."""
    _add(db, "classroom:c/soon", title="Problem set", due_at="2026-09-23T20:59:00Z",
         submission_state="NEW")

    texts = _texts(writer.render_plan(build_plan(db, _rhythm(), MONDAY)))

    assert any("Wed 23 Sep" in t for t in texts)


def test_items_today_do_not_repeat_the_date(db):
    _add(db, "calendar:now", title="Lecture", due_at="2026-09-21T06:00:00Z")

    bullets = [b for b in writer.render_plan(build_plan(db, _rhythm(), MONDAY))
               if b["type"] == "bulleted_list_item"]

    assert _text_of(bullets[0]) == "09:00 Lecture"


# --- links ------------------------------------------------------------------

def test_an_item_links_back_to_google(db):
    _add(db, "classroom:c/1", title="Lab", due_at="2026-09-21T20:59:00Z",
         submission_state="CREATED", link="https://classroom.google.com/c/AAA/a/BBB/details")

    bullet = next(b for b in writer.render_plan(build_plan(db, _rhythm(), MONDAY))
                  if b["type"] == "bulleted_list_item")

    assert bullet["bulleted_list_item"]["rich_text"][0]["text"]["link"] == {
        "url": "https://classroom.google.com/c/AAA/a/BBB/details"
    }


def test_an_item_with_no_link_is_still_rendered(db):
    _add(db, "calendar:now", title="No link", due_at="2026-09-21T06:00:00Z")

    bullet = next(b for b in writer.render_plan(build_plan(db, _rhythm(), MONDAY))
                  if b["type"] == "bulleted_list_item")

    assert "link" not in bullet["bulleted_list_item"]["rich_text"][0]["text"]


# --- Notion's documented limits --------------------------------------------

def test_overlong_text_is_truncated_not_rejected():
    """One huge title must not cost you the whole day's page."""
    element = writer.rich_text("x" * 5000)

    assert len(element["text"]["content"]) == writer.MAX_TEXT_CHARS


def test_an_overlong_url_is_dropped_rather_than_breaking_the_block():
    element = writer.rich_text("title", link="https://example.com/" + "y" * 3000)

    assert "link" not in element["text"]
    assert element["text"]["content"] == "title"


def test_blocks_are_chunked_to_what_notion_accepts():
    """Notion accepts at most 100 children per append request."""
    blocks = [writer.marker_block()] * 250

    batches = writer.chunked(blocks)

    assert [len(b) for b in batches] == [100, 100, 50]
    assert sum(len(b) for b in batches) == 250


def test_a_short_page_is_a_single_request(db):
    blocks = writer.render_plan(build_plan(db, _rhythm(), MONDAY))

    assert len(writer.chunked(blocks)) == 1


# --- the empty day ----------------------------------------------------------

def test_an_empty_day_says_so(db):
    texts = _texts(writer.render_plan(build_plan(db, _rhythm(), MONDAY)))

    assert any("Nothing scheduled, nothing due." in t for t in texts)


def test_an_empty_themeless_day_suggests_setting_a_theme(db):
    texts = _texts(writer.render_plan(build_plan(db, _rhythm(), TUESDAY)))

    assert any("rhythm.toml" in t for t in texts)


def test_a_day_with_items_does_not_claim_to_be_empty(db):
    _add(db, "calendar:now", title="Something", due_at="2026-09-21T06:00:00Z")

    texts = _texts(writer.render_plan(build_plan(db, _rhythm(), MONDAY)))

    assert not any("Nothing scheduled" in t for t in texts)


def test_block_text_reads_both_the_send_and_receive_shapes():
    """Notion sends text.content but returns plain_text for the same thing."""
    sent = writer.marker_block()
    received = _marker_block()          # the shape Notion returns

    assert writer.block_text(sent) == MARKER_TEXT
    assert writer.block_text(received) == MARKER_TEXT
    assert writer.is_agent_marker(sent)
    assert writer.is_agent_marker(received)


# ============================================================================
# Writing. The safety property under test: the agent deletes ONLY blocks it
# recorded writing, so nothing you add to the page can be destroyed.
# ============================================================================

class WritableNotion(FakeNotion):
    """FakeNotion plus append and delete, tracking what is on the page."""

    def __init__(self, blocks=None, append_error=None, delete_errors=None):
        super().__init__(blocks=blocks)
        self.page_blocks = list(blocks or [])
        self.appended: list[dict] = []
        self.deleted: list[str] = []
        self._append_error = append_error
        self._delete_errors = delete_errors or {}
        self._next_id = 0
        outer = self

        class _Children:
            def list(self, block_id, start_cursor=None):
                return {"results": outer.page_blocks, "has_more": False, "next_cursor": None}

            def append(self, block_id, children):
                if outer._append_error:
                    raise outer._append_error
                results = []
                for child in children:
                    outer._next_id += 1
                    made = {**child, "id": f"new-block-{outer._next_id}"}
                    outer.page_blocks.append(made)
                    outer.appended.append(made)
                    results.append(made)
                return {"results": results}

        class _Blocks:
            children = _Children()

            def delete(self, block_id):
                if block_id in outer._delete_errors:
                    raise outer._delete_errors[block_id]
                outer.deleted.append(block_id)
                outer.page_blocks = [b for b in outer.page_blocks if b.get("id") != block_id]
                return {"id": block_id, "archived": True}

        self.blocks = _Blocks()


PAGE = "11111111222233334444555555555555"


def test_the_first_write_appends_and_records_every_block(db):
    client = WritableNotion(blocks=[])

    result = writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)

    assert result["written"] > 0
    assert result["deleted"] == 0
    assert queries.get_notion_blocks(db, PAGE) == [b["id"] for b in client.appended]


def test_a_second_write_removes_exactly_the_first_ones(db):
    client = WritableNotion(blocks=[])

    writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)
    first_ids = list(queries.get_notion_blocks(db, PAGE))

    _add(db, "calendar:new", title="Something new", due_at="2026-09-21T06:00:00Z")
    result = writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)

    assert sorted(client.deleted) == sorted(first_ids)
    assert result["deleted"] == len(first_ids)
    # Only the newest blocks remain recorded, and on the page.
    assert set(queries.get_notion_blocks(db, PAGE)).isdisjoint(first_ids)
    assert all(b["id"] not in first_ids for b in client.page_blocks)


def test_running_twice_leaves_one_copy_not_two(db):
    """Idempotent: the scheduler may run late, or run twice."""
    client = WritableNotion(blocks=[])
    plan = build_plan(db, _rhythm(), MONDAY)

    writer.write_plan(db, client, plan, PAGE)
    first_count = len(client.page_blocks)
    writer.write_plan(db, client, plan, PAGE)

    assert len(client.page_blocks) == first_count


def test_blocks_the_agent_did_not_write_are_never_deleted(db):
    """The core safety property. A note you typed must survive every run."""
    client = WritableNotion(blocks=[])
    writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)

    # You add a note and a sub-page below the agent's content.
    your_note = {"id": "your-note", "type": "paragraph",
                 "paragraph": {"rich_text": [{"plain_text": "my own note"}]}}
    your_subpage = {"id": "your-subpage", "type": "child_page", "child_page": {"title": "Notes"}}
    client.page_blocks.extend([your_note, your_subpage])

    writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)

    assert "your-note" not in client.deleted
    assert "your-subpage" not in client.deleted
    assert your_note in client.page_blocks
    assert your_subpage in client.page_blocks


def test_a_page_of_unknown_content_is_refused_before_writing(db):
    client = WritableNotion(blocks=[_text_block("paragraph", "My thesis notes")])

    with pytest.raises(NotionError, match="no record of writing"):
        writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)

    assert client.appended == []
    assert client.deleted == []


def test_the_agent_keeps_working_after_you_add_a_note(db):
    """End-to-end version of the lockout regression, across two runs."""
    client = WritableNotion(blocks=[])
    writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)

    client.page_blocks.append({"id": "your-note", "type": "paragraph",
                               "paragraph": {"rich_text": [{"plain_text": "MY OWN NOTE"}]}})

    # Two further runs must both succeed and both leave the note alone.
    for _ in range(2):
        result = writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)
        assert result["other_blocks"] == 1

    assert "your-note" not in client.deleted
    assert any(b.get("id") == "your-note" for b in client.page_blocks)


def test_a_failed_append_leaves_the_old_page_intact(db):
    """Appending before deleting means a failure loses nothing."""
    client = WritableNotion(blocks=[])
    writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)
    before = list(client.page_blocks)
    recorded_before = queries.get_notion_blocks(db, PAGE)

    client._append_error = _api_error("internal_server_error")

    with pytest.raises(Exception):
        writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)

    assert client.page_blocks == before          # nothing removed
    assert client.deleted == []
    assert queries.get_notion_blocks(db, PAGE) == recorded_before


def test_a_block_already_deleted_by_hand_is_not_an_error(db):
    client = WritableNotion(blocks=[])
    writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)
    first_ids = queries.get_notion_blocks(db, PAGE)

    # You deleted one of the agent's blocks yourself in Notion.
    client._delete_errors = {first_ids[0]: _api_error("object_not_found")}

    result = writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)

    assert result["already_gone"] == 1
    assert result["deleted"] == len(first_ids) - 1
    # It is forgotten either way - it is gone, which is what we wanted.
    assert first_ids[0] not in queries.get_notion_blocks(db, PAGE)


def test_an_unexpected_delete_error_is_not_swallowed(db):
    client = WritableNotion(blocks=[])
    writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)
    first_ids = queries.get_notion_blocks(db, PAGE)
    client._delete_errors = {first_ids[0]: _api_error("rate_limited")}

    with pytest.raises(Exception):
        writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)


def test_a_long_plan_is_appended_in_batches(db):
    for i in range(120):
        _add(db, f"calendar:e{i}", title=f"Event {i}",
             due_at=f"2026-09-21T{6 + i % 12:02d}:00:00Z")

    client = WritableNotion(blocks=[])
    result = writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE)

    assert result["written"] > 100        # more than one request was needed
    assert len(queries.get_notion_blocks(db, PAGE)) == result["written"]


# --- warnings on the page ---------------------------------------------------

def test_stale_data_is_flagged_on_the_page_not_just_the_terminal(db):
    """The gap that mattered: the terminal said STALE, the page did not.
    Notion is the surface actually looked at."""
    from src.rhythm.today import STALE_AFTER_SECONDS

    plan = build_plan(db, _rhythm(), MONDAY)
    plan.data_age_seconds = STALE_AFTER_SECONDS + 1

    blocks = writer.render_plan(plan)

    warnings = [b for b in blocks if b["type"] == "callout"
                and b["callout"].get("color") == "red_background"]
    assert len(warnings) == 1
    assert "not today's data" in _text_of(warnings[0])
    assert "python -m src.run_daily" in _text_of(warnings[0])


def test_fresh_data_gets_no_warning(db):
    plan = build_plan(db, _rhythm(), MONDAY)
    plan.data_age_seconds = 30

    blocks = writer.render_plan(plan)

    assert not [b for b in blocks if b["type"] == "callout"
                and b["callout"].get("color") == "red_background"]


def test_never_synced_counts_as_stale(db):
    plan = build_plan(db, _rhythm(), MONDAY)
    plan.data_age_seconds = None

    texts = _texts(writer.render_plan(plan))

    assert any("not today's data" in t for t in texts)


def test_a_signin_warning_appears_at_the_very_top(db):
    """Above everything, because it explains why the rest may be wrong."""
    plan = build_plan(db, _rhythm(), MONDAY)
    plan.data_age_seconds = 30

    blocks = writer.render_plan(plan, warnings=["Google needs you to sign in again"])

    assert blocks[0]["type"] == "callout"
    assert "sign in again" in _text_of(blocks[0])
    assert blocks[0]["callout"]["color"] == "red_background"


def test_warnings_and_staleness_can_both_appear(db):
    from src.rhythm.today import STALE_AFTER_SECONDS

    plan = build_plan(db, _rhythm(), MONDAY)
    plan.data_age_seconds = STALE_AFTER_SECONDS + 1

    blocks = writer.render_plan(plan, warnings=["Google needs you to sign in again"])
    reds = [b for b in blocks if b["type"] == "callout"
            and b["callout"].get("color") == "red_background"]

    assert len(reds) == 2
    assert "sign in" in _text_of(reds[0])        # the cause comes first
    assert "not today's data" in _text_of(reds[1])


def test_warnings_are_written_through_to_the_page(db):
    client = WritableNotion(blocks=[])

    writer.write_plan(db, client, build_plan(db, _rhythm(), MONDAY), PAGE,
                      warnings=["Google needs you to sign in again"])

    assert any("sign in again" in writer.block_text(b) for b in client.page_blocks)


def test_the_later_section_reaches_the_page(db):
    """Regression: work nine days out was invisible in Notion."""
    _add(db, "classroom:c/far", title="Assignment 1", due_at="2026-10-08T20:59:00Z",
         submission_state="CREATED")

    texts = _texts(writer.render_plan(build_plan(db, _rhythm(), MONDAY)))

    assert any(t.startswith("Later (1)") for t in texts)
    assert any("Assignment 1" in t for t in texts)
    assert not any("Nothing scheduled" in t for t in texts)


def test_later_comes_after_the_urgent_sections(db):
    _add(db, "classroom:c/now", title="Due today", due_at="2026-09-21T20:59:00Z",
         submission_state="CREATED")
    _add(db, "classroom:c/far", title="Far off", due_at="2026-11-30T20:59:00Z",
         submission_state="CREATED")

    texts = _texts(writer.render_plan(build_plan(db, _rhythm(), MONDAY)))
    headings = [i for i, t in enumerate(texts) if t.startswith(("Today (", "Later ("))]

    assert texts[headings[0]].startswith("Today")
    assert texts[headings[1]].startswith("Later")
