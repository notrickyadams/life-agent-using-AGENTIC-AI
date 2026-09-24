"""Tests for src/notion/writer.py - connection checks and the overwrite guard.

No network: a fake Notion client stands in for the real one. The guard is
the important part - it is what stops a mistyped page id from quietly
emptying a page you actually use.
"""

import pytest

from src import config
from src.notion import writer
from src.notion.writer import MARKER_TEXT, NotionError


def _text_block(kind: str, text: str) -> dict:
    return {"type": kind, kind: {"rich_text": [{"plain_text": text}]}}


def _marker_block() -> dict:
    return _text_block("callout", MARKER_TEXT)


def _page(title="Life Agent", archived=False, in_trash=False) -> dict:
    return {
        "object": "page",
        "id": "3e576208-b43c-80dd-ba62-e85133b6bf05",
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
    monkeypatch.setattr(config, "NOTION_PARENT_ID", "3e576208b43c80ddba62e85133b6bf05")


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
