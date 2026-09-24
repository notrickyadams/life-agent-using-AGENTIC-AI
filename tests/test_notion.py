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


def test_a_page_the_agent_already_owns_is_safe_to_rewrite():
    blocks = [_marker_block(), _text_block("heading_1", "Wednesday")]

    summary = writer.check_page(FakeNotion(blocks=blocks))

    assert summary["ownership"] == "agent"
    assert summary["blocks"] == 2


def test_a_page_with_your_own_content_is_refused():
    """The whole point of the guard: never destroy something you typed."""
    blocks = [_text_block("paragraph", "My thesis notes, please do not delete")]

    with pytest.raises(NotionError) as excinfo:
        writer.check_page(FakeNotion(blocks=blocks))

    message = str(excinfo.value)
    assert "did not write" in message
    assert "NOTION_PARENT_ID" in message      # tells you how to fix it
    assert "thesis notes" in message          # shows what it found


def test_the_marker_must_be_the_first_block():
    """Content above the marker means someone typed at the top of the page."""
    blocks = [_text_block("paragraph", "something I added"), _marker_block()]

    with pytest.raises(NotionError, match="did not write"):
        writer.check_page(FakeNotion(blocks=blocks))


def test_a_lookalike_marker_of_the_wrong_type_does_not_count():
    blocks = [_text_block("paragraph", MARKER_TEXT)]

    with pytest.raises(NotionError, match="did not write"):
        writer.check_page(FakeNotion(blocks=blocks))


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

def test_the_marker_is_always_the_first_block(db):
    """check_page() identifies the agent's page by this, so it must lead."""
    blocks = writer.render_plan(build_plan(db, _rhythm(), MONDAY))

    assert blocks[0]["type"] == "callout"
    assert writer.is_agent_marker(blocks[0])


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
