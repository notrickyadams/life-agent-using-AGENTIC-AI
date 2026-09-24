"""
Entry point - the one script the scheduler runs.

Stages, in order:
    1. collect   sync Google Calendar and Classroom into SQLite   (V1 step 1)
    2. plan      work out what is relevant today, by rhythm       (V1 step 2)

Later steps add the local model, the single Claude call, and the Notion
write - in that order, behind this same file.

Each stage is isolated: a collector failing does not stop the other, and
neither stops the plan being built from whatever is already stored.

Usage:
    python -m src.run_daily             normal run
    python -m src.run_daily --dry-run   fetch, parse and plan, write NOTHING
    python -m src.run_daily --verbose   more detail in the console
    python -m src.run_daily --quiet     no plan printout (the log still has it)
    python -m src.run_daily --no-browser  for scheduled runs: fail rather than
                                          wait at a browser sign-in prompt

Exit codes (the scheduler reads these):
    0  everything succeeded
    1  at least one stage failed (the rest still ran; see logs/agent.log)
    2  configuration problem - nothing was attempted
"""

import argparse
import logging
import sys
from collections.abc import Callable

from src import config
from src.collectors import calendar, classroom
from src.db import queries, schema
from src.rhythm import today as rhythm_today
from src.rhythm.config import RhythmError, load_rhythm

log = logging.getLogger(__name__)

# name -> function returning rows for the events table. Kept as data so
# tests can swap in fakes and so adding a source later is one line.
Collector = Callable[..., list[dict]]
COLLECTORS: dict[str, Collector] = {
    "calendar": calendar.collect_calendar,
    "classroom": classroom.collect_classroom,
}


def setup_logging(verbose: bool = False) -> None:
    """Log to logs/agent.log (always, with timestamps) and to the console.

    The file is what you read after an overnight run; the console is for
    when you run it by hand. Both are UTF-8 so course names print correctly.
    """
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = logging.FileHandler(config.LOG_DIR / "agent.log", encoding="utf-8")
    file_handler.setFormatter(fmt)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)

    # The Google client library is chatty at INFO; keep it to warnings.
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)

    # google-auth warns on every refresh that Google reported the Classroom
    # scope under an alias (student-submissions.me instead of coursework.me).
    # Verified benign - the granted scopes are logged at sign-in, and the
    # calls they cover work. Silenced so a real warning is not lost in it.
    logging.getLogger("google.oauth2.credentials").setLevel(logging.ERROR)

    # notion-client logs a line per HTTP request; keep the daily log readable.
    for name in ("notion_client", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)


def run_pipeline(
    conn,
    collectors: dict[str, Collector],
    dry_run: bool = False,
    allow_browser: bool = True,
) -> dict[str, dict]:
    """Run every collector, each isolated from the others, and record the run.

    Returns {name: {"ok": bool, "rows": int, "inactive": int, "error": str|None}}.
    A collector that raises is logged with its traceback and marked failed;
    the remaining collectors still run.

    dry_run writes nothing that outlives the call - no token file, no rows,
    no runs row. Fetched rows ARE loaded into an open transaction so the
    plan reflects what was just fetched rather than what happened to be
    stored, and that transaction is rolled back before returning.

    allow_browser=False is for scheduled runs: a sign-in that would need a
    browser is recorded as a failure instead of hanging forever.
    """
    started_at = queries.utc_now_iso()
    run_id = None if dry_run else queries.start_run(conn, started_at)
    if dry_run:
        log.info("DRY RUN - nothing will be kept: no rows, no token file, no run record")

    results: dict[str, dict] = {}
    for name, collect in collectors.items():
        outcome = {"ok": False, "rows": 0, "inactive": 0, "error": None}
        try:
            # A dry run must not refresh-and-save a token file either.
            rows = collect(allow_browser=allow_browser, save_token=not dry_run)
            outcome["rows"] = len(rows)
            if dry_run:
                # Load into the open transaction so the plan below sees this
                # data; rolled back at the end, so nothing is kept.
                for row in rows:
                    queries.upsert_event(conn, row)
            else:
                queries.upsert_events(conn, rows)
                # Anything from this source that Google did NOT return this
                # time is flagged, never deleted. Only safe after success.
                outcome["inactive"] = queries.mark_unseen_inactive(conn, name, started_at)
                # Stamped here, the moment this source succeeded, so the
                # plan built below sees THIS run rather than the last one.
                queries.record_source_synced(conn, run_id, name)
            outcome["ok"] = True
            log.info("%s: %d rows%s", name, len(rows),
                     f", {outcome['inactive']} now inactive" if outcome["inactive"] else "")
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point here
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            log.exception("%s collector failed", name)
        results[name] = outcome

    # --- stage 2: the day plan ----------------------------------------------
    # Built from whatever is in the database, so it still works when a
    # collector failed - you get a plan from the last good sync rather than
    # nothing at all. A broken rhythm.toml is reported, never fatal: the
    # sync work above is already done and must not be lost.
    plan_outcome = {"ok": False, "items": None, "error": None}
    try:
        rhythm = load_rhythm()
        plan = rhythm_today.build_plan(conn, rhythm)
        plan_outcome["items"] = plan.total
        plan_outcome["ok"] = True
        results["plan"] = plan_outcome
        results["_plan"] = plan          # the object itself, for the caller to print

        if not dry_run:
            tags = rhythm_today.write_rule_tags(conn, plan)
            log.info("plan: %d items for %s (%s), %d rule tags",
                     plan.total, plan.on,
                     plan.day.theme if plan.day.has_theme else "no theme", tags)
        else:
            log.info("plan: %d items for %s (tags not written)", plan.total, plan.on)
    except RhythmError as exc:
        plan_outcome["error"] = f"rhythm.toml: {exc}"
        log.error("plan stage failed - %s", plan_outcome["error"])
        results["plan"] = plan_outcome
    except Exception as exc:  # noqa: BLE001 - same isolation rule as collectors
        plan_outcome["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("plan stage failed")
        results["plan"] = plan_outcome

    if dry_run:
        # Discard everything the dry run loaded, including the rows the plan
        # was built from. Nothing reaches disk.
        conn.rollback()

    if not dry_run:
        errors = [
            f"{n}: {r['error']}"
            for n, r in results.items()
            if not n.startswith("_") and r.get("error")
        ]
        queries.finish_run(
            conn,
            run_id,
            calendar_ok=results.get("calendar", {}).get("ok"),
            classroom_ok=results.get("classroom", {}).get("ok"),
            # Counted from what actually happened, not asserted. Nothing
            # calls Claude before step 5, so this reads 0 today - but it
            # will keep telling the truth once step 5 exists.
            claude_calls_made=queries.count_claude_calls(conn, run_id),
            plan_items=plan_outcome["items"],
            error="; ".join(errors) or None,
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily sync: Google Calendar + Classroom -> SQLite")
    parser.add_argument("--dry-run", action="store_true", help="fetch, parse and plan, but write nothing")
    parser.add_argument("--verbose", action="store_true", help="debug-level logging")
    parser.add_argument("--quiet", action="store_true", help="do not print the plan (the log still has it)")
    parser.add_argument("--no-browser", action="store_true",
                        help="never open a browser to sign in; fail instead (use this for scheduled runs)")
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    setup_logging(args.verbose)

    problems = config.check()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        return 2

    conn = schema.connect()
    added = schema.init_db(conn)
    if added:
        log.info("database updated: added %s", ", ".join(added))
    try:
        results = run_pipeline(conn, COLLECTORS, dry_run=args.dry_run,
                               allow_browser=not args.no_browser)
    finally:
        conn.close()

    plan = results.pop("_plan", None)
    if plan is not None and not args.quiet:
        print()
        print(rhythm_today.format_plan(plan))
        print()

    summary = ", ".join(
        f"{name} {'OK' if r['ok'] else 'FAILED'}" for name, r in results.items()
    )
    all_ok = all(r["ok"] for r in results.values())
    log.info("run finished: %s", summary)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
