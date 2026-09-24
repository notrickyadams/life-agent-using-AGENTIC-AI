"""
Writes the daily view into Notion. Write-only: nothing read from Notion
feeds back into the pipeline.

Step 3a is this file's connection check. Rendering and writing arrive in
3b and 3c; this part only proves the credentials and the page work, and
sets up the safety rule everything else depends on.

THE SAFETY RULE
    The page is rewritten on every run, so its whole content must be
    content this agent wrote. Before replacing anything, check_page()
    looks for blocks the agent did not create. If it finds any, the write
    is refused rather than destroying something you typed. A mistyped page
    id therefore fails loudly instead of quietly emptying a real page.

    The agent marks its own pages by making the FIRST block a specific
    callout (see MARKER_TEXT). An empty page is fine too - that is what a
    freshly made page looks like.

Check the connection any time with:
    python -m src.notion.writer --check
"""

import argparse
import logging
import sys

from src import config

log = logging.getLogger(__name__)

# The first block of a page the agent owns. Anything else at the top means
# the page holds someone else's content and must not be overwritten.
MARKER_TEXT = "Written automatically by your agent. Anything typed here is replaced on the next run."


class NotionError(Exception):
    """Raised when Notion is misconfigured, unreachable, or unsafe to write to."""


def quiet_http_logs() -> None:
    """Stop notion-client logging one line per HTTP request.

    Useful when debugging, noise otherwise - and noise in a daily log is
    how a real warning gets missed.
    """
    for name in ("notion_client", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_client():
    """Build a Notion client from the configured token.

    Imported lazily so the rest of the pipeline still runs when notion-client
    is not installed, or when Notion is simply not configured yet.
    """
    if not config.NOTION_API_KEY:
        raise NotionError(
            "NOTION_API_KEY is not set in .env. Create an integration at "
            "https://www.notion.so/my-integrations and copy its internal secret."
        )
    try:
        from notion_client import Client
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise NotionError("notion-client is not installed. Run: pip install -r requirements.txt") from exc

    return Client(auth=config.NOTION_API_KEY)


def describe_connection(client) -> dict:
    """Who this token authenticates as. Proves the token itself is valid."""
    from notion_client.errors import APIResponseError

    try:
        me = client.users.me()
    except APIResponseError as exc:
        raise NotionError(
            f"Notion rejected the token ({exc.code}). Check NOTION_API_KEY in .env - "
            "it should be the integration's internal secret, starting 'ntn_'."
        ) from exc

    bot = me.get("bot") or {}
    return {
        "name": me.get("name"),
        "workspace": bot.get("workspace_name"),
        "owner_type": (bot.get("owner") or {}).get("type"),
    }


def page_title(page: dict) -> str:
    """The page's title, or '(untitled)'.

    A page's title lives in whichever property has type 'title', which is
    not always called "title" - so it is found by type, not by name.
    """
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            text = "".join(part.get("plain_text", "") for part in prop.get("title", []))
            if text.strip():
                return text.strip()
    return "(untitled)"


def get_page(client, page_id: str | None = None) -> dict:
    """Fetch the target page, with a clear message for the usual failures."""
    from notion_client.errors import APIResponseError

    page_id = page_id or config.NOTION_PARENT_ID
    if not page_id:
        raise NotionError("NOTION_PARENT_ID is not set in .env (the 32-character id from the page URL).")

    try:
        page = client.pages.retrieve(page_id=page_id)
    except APIResponseError as exc:
        if exc.code == "object_not_found":
            raise NotionError(
                f"Notion cannot see page {page_id}. Either the id is wrong, or the "
                "integration has not been connected to it. In Notion open the page, "
                "then: ... menu -> Connections -> Connect to -> your integration."
            ) from exc
        raise NotionError(f"Notion refused the page request ({exc.code}): {exc}") from exc

    if page.get("archived") or page.get("in_trash"):
        raise NotionError(f"Page {page_id} is in the Notion trash. Restore it, or use a different page.")

    return page


def list_blocks(client, page_id: str) -> list[dict]:
    """Every top-level block on the page, following pagination."""
    blocks: list[dict] = []
    cursor = None
    while True:
        response = client.blocks.children.list(block_id=page_id, start_cursor=cursor)
        blocks.extend(response.get("results", []))
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return blocks


def block_text(block: dict) -> str:
    """The plain text of a block, or '' for blocks that have none.

    Notion's API is asymmetric here: a block you SEND carries the text at
    text.content, while a block you READ BACK carries it at plain_text. This
    reads either, so the same function works on a block we just built and on
    one Notion returned - which is what lets the marker we write be
    recognised by the check that looks for it.
    """
    body = block.get(block.get("type", ""), {})
    if not isinstance(body, dict):
        return ""
    pieces = []
    for part in body.get("rich_text", []):
        if part.get("plain_text"):
            pieces.append(part["plain_text"])
        else:
            pieces.append((part.get("text") or {}).get("content", ""))
    return "".join(pieces)


def is_agent_marker(block: dict) -> bool:
    """True when this block is the marker the agent writes at the top of its page."""
    return block.get("type") == "callout" and block_text(block).strip() == MARKER_TEXT


def check_page(client, page_id: str | None = None) -> dict:
    """Confirm the page exists and is safe to rewrite.

    Returns a summary. Raises NotionError when the page holds content the
    agent did not write - refusing is the whole point: a mistyped id must
    not quietly empty a real page.
    """
    page_id = page_id or config.NOTION_PARENT_ID
    page = get_page(client, page_id)
    blocks = list_blocks(client, page_id)

    if not blocks:
        ownership = "empty"          # a fresh page: safe
    elif is_agent_marker(blocks[0]):
        ownership = "agent"          # ours: safe to replace
    else:
        raise NotionError(
            f"Page {page_title(page)!r} already has {len(blocks)} block(s) that this agent "
            "did not write, so rewriting it would destroy them.\n"
            "  - If this is the wrong page, fix NOTION_PARENT_ID in .env.\n"
            "  - If you want the agent to own this page, clear it in Notion first.\n"
            f"  First block is a {blocks[0].get('type')}: {block_text(blocks[0])[:60]!r}"
        )

    return {
        "page_id": page["id"],
        "title": page_title(page),
        "url": page.get("url"),
        "blocks": len(blocks),
        "ownership": ownership,
        "safe_to_write": True,
    }


# --- rendering (pure - builds blocks, talks to nothing) ---------------------
#
# Notion's documented limits, which the helpers below respect:
#   https://developers.notion.com/reference/request-limits
#   https://developers.notion.com/reference/patch-block-children
MAX_TEXT_CHARS = 2000        # one rich_text content field
MAX_URL_CHARS = 2000
MAX_RICH_TEXT_ITEMS = 100    # elements in a rich_text array
MAX_CHILDREN_PER_APPEND = 100

# Section headings, in the order they appear on the page.
SECTION_TITLES = {
    "overdue": "Overdue",
    "today": "Today",
    "due_soon": "Due soon",
    "themed": "Fits today's theme",
}


def rich_text(content: str, link: str | None = None, bold: bool = False) -> dict:
    """One rich_text element, truncated to what Notion accepts.

    Truncating rather than letting the API reject the whole page: one
    over-long title must not cost you the entire day's view.
    """
    text: dict = {"content": content[:MAX_TEXT_CHARS]}
    if link and len(link) <= MAX_URL_CHARS:
        text["link"] = {"url": link}
    return {"type": "text", "text": text, "annotations": {"bold": bold}}


def _block(kind: str, *rich: dict, **extra) -> dict:
    """A Notion block of the given type holding these rich_text elements."""
    body = {"rich_text": list(rich)[:MAX_RICH_TEXT_ITEMS], **extra}
    return {"object": "block", "type": kind, kind: body}


def marker_block() -> dict:
    """The callout that identifies this page as the agent's.

    check_page() looks for exactly this as the first block, and it tells you
    in plain words that the page is replaced - so nothing typed here is lost
    without warning.
    """
    return _block(
        "callout",
        rich_text(MARKER_TEXT),
        icon={"type": "emoji", "emoji": "🤖"},
        color="gray_background",
    )


def item_block(item, with_date: bool = False) -> dict:
    """One plan item as a bullet, linking back to Google where possible."""
    return _block("bulleted_list_item", rich_text(item.describe(with_date=with_date), link=item.link))


def render_plan(plan) -> list[dict]:
    """Turn a DayPlan into the blocks that make up the page.

    Pure: builds a list and returns it. Nothing here touches Notion, so the
    whole layout is testable without a network call or a real page.
    """
    from src.rhythm.today import describe_age

    blocks: list[dict] = [marker_block()]

    heading = plan.on.strftime("%A %d %B %Y")
    if plan.day.has_theme:
        heading += f"  -  {plan.day.theme}"
    blocks.append(_block("heading_1", rich_text(heading)))

    # Always state how current the underlying Google data is, so the page
    # never silently implies "just now".
    age = describe_age(plan.data_age_seconds)
    blocks.append(_block("paragraph", rich_text(f"Google data: {age}")))

    if plan.day.focus:
        blocks.append(_block("quote", rich_text(plan.day.focus)))

    for key, title in SECTION_TITLES.items():
        items = getattr(plan, key)
        if not items:
            continue
        label = f"{title} ({len(items)})"
        if key == "themed" and plan.day.has_theme:
            label = f"{title}: {plan.day.theme} ({len(items)})"
        blocks.append(_block("heading_2", rich_text(label)))
        # Everything outside Today belongs to another day, so name that day.
        with_date = key != "today"
        blocks.extend(item_block(item, with_date=with_date) for item in items)

    if plan.is_empty:
        blocks.append(_block("paragraph", rich_text("Nothing scheduled, nothing due.")))
        if not plan.day.has_theme:
            blocks.append(_block(
                "paragraph",
                rich_text(f"No theme set for {plan.day.name} - add one in rhythm.toml if you want one."),
            ))

    return blocks


def chunked(blocks: list[dict], size: int = MAX_CHILDREN_PER_APPEND) -> list[list[dict]]:
    """Split blocks into batches Notion will accept (100 children per append)."""
    return [blocks[i:i + size] for i in range(0, len(blocks), size)]


def _main() -> int:
    parser = argparse.ArgumentParser(description="Notion writer - connection check (reads only, writes nothing)")
    parser.add_argument("--check", action="store_true", help="verify the token and the target page")
    parser.add_argument("--preview", action="store_true",
                        help="render today's plan to Notion blocks and print them; writes nothing")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    quiet_http_logs()

    if args.preview:
        from src.db import schema
        from src.rhythm.config import load_rhythm
        from src.rhythm.today import build_plan

        conn = schema.connect()
        schema.init_db(conn)
        try:
            blocks = render_plan(build_plan(conn, load_rhythm()))
        finally:
            conn.close()

        print(f"{len(blocks)} blocks, {len(chunked(blocks))} append request(s):")
        print()
        for i, block in enumerate(blocks, 1):
            kind = block["type"]
            parts = block[kind].get("rich_text", [])
            text = "".join(part["text"]["content"] for part in parts)
            link = next(
                (part["text"]["link"]["url"] for part in parts if part["text"].get("link")),
                None,
            )
            line = f"  {i:2}. {kind:20} {text[:70]!r}"
            print(line + (f"  -> {link[:50]}" if link else ""))
        print()
        print("Nothing was written.")
        return 0

    if not args.check:
        parser.print_help()
        return 0

    if not config.notion_configured():
        print("Notion is not configured. Set NOTION_API_KEY and NOTION_PARENT_ID in .env.")
        return 2

    try:
        client = get_client()
        who = describe_connection(client)
        print(f"token      OK - integration {who['name']!r}"
              + (f" in workspace {who['workspace']!r}" if who["workspace"] else ""))

        summary = check_page(client)
        print(f"page       OK - {summary['title']!r}")
        print(f"           id     {summary['page_id']}")
        print(f"           url    {summary['url']}")
        print(f"           blocks {summary['blocks']} ({summary['ownership']})")
        print("\nSafe to write. Nothing was written by this check.")
        return 0
    except NotionError as exc:
        print(f"\nNOT READY:\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(_main())
