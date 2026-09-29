"""
Writes the daily view into Notion. Write-only: nothing read from Notion
feeds back into the pipeline.

Step 3a is this file's connection check. Rendering and writing arrive in
3b and 3c; this part only proves the credentials and the page work, and
sets up the safety rule everything else depends on.

THE SAFETY RULE
    The agent records the id of every block it creates. On a later run it
    deletes exactly those ids and nothing else, so a note, a sub-page or a
    database you add to the page can never be destroyed by it - they were
    never on the list.

    It refuses to touch a page that already has content it has no record of
    writing, so a mistyped page id fails loudly instead of quietly emptying
    a real page. An empty page is fine: that is what a fresh one looks like.

    The MARKER_TEXT callout is a label, telling you what the page is. It is
    deliberately NOT the safety mechanism: appends land at the end of a
    page, so the moment you add a note of your own the marker stops being
    first, and a marker-position rule would lock the agent out of its own
    page.

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


def check_page(client, page_id: str | None = None, known_block_ids: list[str] | None = None) -> dict:
    """Confirm the page exists and is safe to write to.

    Safety comes from the record of what the agent previously wrote, not
    from what the page looks like:

      - the agent has written here before  -> safe. It will delete exactly
        those recorded blocks and leave everything else alone.
      - the page is empty                  -> safe. Nothing to lose.
      - neither                            -> REFUSE. Content the agent has
        no record of writing, so a mistyped page id would destroy it.

    An earlier version required the agent's marker to be the FIRST block.
    That was a heuristic from before block ids were recorded, and it was
    wrong: appending happens at the end of a page, so as soon as you add a
    note of your own the marker stops being first and the agent locks
    itself out of its own page. The recorded ids are exact, so the position
    of anything on the page no longer matters.

    Returns a summary. `known_block_ids` is what the agent recorded writing
    to this page; pass it whenever a database connection is available.
    """
    page_id = page_id or config.NOTION_PARENT_ID
    page = get_page(client, page_id)
    blocks = list_blocks(client, page_id)
    known = set(known_block_ids or [])

    ours = [b for b in blocks if b.get("id") in known]
    theirs = [b for b in blocks if b.get("id") not in known]

    if ours:
        ownership = "agent"      # written here before: the record is authoritative
    elif known:
        # A record exists but none of it is still on the page - you cleared
        # it yourself. Safe, and worth distinguishing from a page the agent
        # has never touched.
        ownership = "cleared"
    elif not blocks:
        ownership = "empty"      # a fresh page, never written to
    else:
        raise NotionError(
            f"Page {page_title(page)!r} has {len(blocks)} block(s), and this agent has no "
            "record of writing any of them - so it will not touch this page.\n"
            "  - If this is the wrong page, fix NOTION_PARENT_ID in .env.\n"
            "  - If you want the agent to use this page, clear it in Notion first.\n"
            f"  First block is a {blocks[0].get('type')}: {block_text(blocks[0])[:60]!r}"
        )

    return {
        "page_id": page["id"],
        "title": page_title(page),
        "url": page.get("url"),
        "blocks": len(blocks),
        "agent_blocks": len(ours),
        "other_blocks": len(theirs),
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


def warning_block(text: str) -> dict:
    """A callout that is hard to miss.

    Used for anything meaning "what you are reading may be wrong". Red,
    because the failure it reports is silent by nature: a page showing
    three-day-old data looks exactly like a page showing today's.
    """
    return _block(
        "callout",
        rich_text(text),
        icon={"type": "emoji", "emoji": "⚠️"},
        color="red_background",
    )


def render_plan(plan, warnings: list[str] | None = None) -> list[dict]:
    """Turn a DayPlan into the blocks that make up the page.

    Pure: builds a list and returns it. Nothing here touches Notion, so the
    whole layout is testable without a network call or a real page.

    `warnings` are things the run could not do - most importantly, that
    Google needs you to sign in again. They go at the very top, above the
    marker, because a page that quietly shows stale data is the one failure
    mode you would never notice on your own.
    """
    from src.rhythm.today import STALE_AFTER_SECONDS, describe_age

    blocks: list[dict] = []

    for text in warnings or []:
        blocks.append(warning_block(text))

    # Stale data gets its own warning even when nothing reported an error -
    # a laptop that was simply off for three days produces no failure at all,
    # just an old page.
    age_seconds = plan.data_age_seconds
    if age_seconds is None or age_seconds > STALE_AFTER_SECONDS:
        blocks.append(warning_block(
            f"This is not today's data - Google was last read {describe_age(age_seconds)}. "
            "Run: python -m src.run_daily"
        ))

    blocks.append(marker_block())

    heading = plan.on.strftime("%A %d %B %Y")
    if plan.day.has_theme:
        heading += f"  -  {plan.day.theme}"
    blocks.append(_block("heading_1", rich_text(heading)))

    # Always state how current the underlying Google data is, so the page
    # never silently implies "just now".
    blocks.append(_block("paragraph", rich_text(f"Google data: {describe_age(age_seconds)}")))

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


# --- writing ----------------------------------------------------------------

def append_blocks(client, page_id: str, blocks: list[dict]) -> list[str]:
    """Append blocks to the page and return the ids Notion assigned them.

    Sent in batches of 100, which is Notion's documented per-request limit.
    """
    created: list[str] = []
    for batch in chunked(blocks):
        response = client.blocks.children.append(block_id=page_id, children=batch)
        created.extend(item["id"] for item in response.get("results", []))
    return created


def delete_blocks(client, block_ids: list[str]) -> tuple[list[str], list[str]]:
    """Delete these blocks. Returns (deleted, already_gone).

    A block you deleted by hand in Notion is not an error - it is simply
    gone, which is the outcome we wanted. Anything else is re-raised.
    """
    from notion_client.errors import APIResponseError

    deleted: list[str] = []
    already_gone: list[str] = []
    for block_id in block_ids:
        try:
            client.blocks.delete(block_id=block_id)
            deleted.append(block_id)
        except APIResponseError as exc:
            if exc.code in ("object_not_found", "validation_error"):
                already_gone.append(block_id)
            else:
                raise
    return deleted, already_gone


def write_plan(conn, client, plan, page_id: str | None = None,
               warnings: list[str] | None = None) -> dict:
    """Replace the agent's content on the page with this plan.

    The order matters. New blocks are appended FIRST, then the previously
    recorded ones are deleted. If the append fails, the old page is still
    intact and nothing has been lost; only a failure after a successful
    append can leave both copies, which is visible and fixable. Deleting
    first would risk an empty page.

    Only blocks this agent recorded writing are deleted. Anything you add to
    the page - a note, a sub-page, a database - is never touched, because it
    was never on that list.

    `warnings` are surfaced at the top of the page - see render_plan.

    Returns a summary of what happened.
    """
    from src.db import queries

    page_id = page_id or config.NOTION_PARENT_ID
    previous = queries.get_notion_blocks(conn, page_id)

    # Refuses if the page holds content the agent has no record of writing.
    summary = check_page(client, page_id, known_block_ids=previous)

    blocks = render_plan(plan, warnings)
    created = append_blocks(client, page_id, blocks)
    queries.record_notion_blocks(conn, page_id, created)

    deleted, already_gone = delete_blocks(client, previous)
    queries.forget_notion_blocks(conn, previous)

    log.info(
        "notion: wrote %d blocks, removed %d previous%s",
        len(created), len(deleted),
        f" ({len(already_gone)} already gone)" if already_gone else "",
    )
    return {
        "written": len(created),
        "deleted": len(deleted),
        "already_gone": len(already_gone),
        "page_id": page_id,
        "url": summary["url"],
        "other_blocks": summary["other_blocks"],
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Notion writer - connection check (reads only, writes nothing)")
    parser.add_argument("--check", action="store_true", help="verify the token and the target page")
    parser.add_argument("--preview", action="store_true",
                        help="render today's plan to Notion blocks and print them; writes nothing")
    parser.add_argument("--write", action="store_true",
                        help="actually write today's plan to the Notion page")
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

    if args.write:
        from src.db import schema
        from src.rhythm.config import load_rhythm
        from src.rhythm.today import build_plan

        conn = schema.connect()
        schema.init_db(conn)
        try:
            plan = build_plan(conn, load_rhythm())
            result = write_plan(conn, get_client(), plan)
        except NotionError as exc:
            print(f"\nNOT WRITTEN:\n{exc}")
            return 1
        finally:
            conn.close()

        print(f"wrote {result['written']} blocks, removed {result['deleted']} previous")
        if result["other_blocks"]:
            print(f"left {result['other_blocks']} block(s) of your own untouched")
        print(f"page: {result['url']}")
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

        from src.db import schema
        from src.db import queries as db_queries

        conn = schema.connect()
        schema.init_db(conn)
        try:
            known = db_queries.get_notion_blocks(conn, config.NOTION_PARENT_ID)
        finally:
            conn.close()

        summary = check_page(client, known_block_ids=known)
        print(f"page       OK - {summary['title']!r}")
        print(f"           id     {summary['page_id']}")
        print(f"           url    {summary['url']}")
        print(f"           blocks {summary['blocks']} ({summary['ownership']}"
              + (f", {summary['other_blocks']} yours" if summary["other_blocks"] else "") + ")")
        print("\nSafe to write. Nothing was written by this check.")
        return 0
    except NotionError as exc:
        print(f"\nNOT READY:\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(_main())
