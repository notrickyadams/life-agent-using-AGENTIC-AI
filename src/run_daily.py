"""
Entry point - the one script the scheduler runs.

Phase 0 (V1 step 1): sync Google Calendar and Classroom into SQLite and
record the run. Later steps add rhythm filtering, the local model, the
single Claude call, and the Notion write - in that order, behind this file.

Usage:
    python -m src.run_daily             normal run
    python -m src.run_daily --dry-run   fetch + parse, write NOTHING (no DB rows, no runs row)
    python -m src.run_daily --verbose   more detail in the console

Exit codes (the scheduler reads these):
    0  every collector succeeded
    1  at least one collector failed (the others still ran; see logs/agent.log)
    2  configuration problem - nothing was attempted
"""

import argparse
import logging
import sys
from collections.abc import Callable

from src import config
from src.collectors import calendar, classroom
from src.db import queries, schema

log = logging.getLogger(__name__)

# name -> function returning rows for the events table. Kept as data so
# tests can swap in fakes and so adding a source later is one line.
Collector = Callable[[], list[dict]]
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


def run_pipeline(conn, collectors: dict[str, Collector], dry_run: bool = False) -> dict[str, dict]:
    """Run every collector, each isolated from the others, and record the run.

    Returns {name: {"ok": bool, "rows": int, "inactive": int, "error": str|None}}.
    A collector that raises is logged with its traceback and marked failed;
    the remaining collectors still run. With dry_run, nothing is written.
    """
    started_at = queries.utc_now_iso()
    run_id = None if dry_run else queries.start_run(conn, started_at)
    if dry_run:
        log.info("DRY RUN - fetching and parsing only, nothing will be written")

    results: dict[str, dict] = {}
    for name, collect in collectors.items():
        outcome = {"ok": False, "rows": 0, "inactive": 0, "error": None}
        try:
            rows = collect()
            outcome["rows"] = len(rows)
            if not dry_run:
                queries.upsert_events(conn, rows)
                # Anything from this source that Google did NOT return this
                # time is flagged, never deleted. Only safe after success.
                outcome["inactive"] = queries.mark_unseen_inactive(conn, name, started_at)
            outcome["ok"] = True
            log.info("%s: %d rows%s", name, len(rows),
                     f", {outcome['inactive']} now inactive" if outcome["inactive"] else "")
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point here
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            log.exception("%s collector failed", name)
        results[name] = outcome

    if not dry_run:
        errors = [f"{n}: {r['error']}" for n, r in results.items() if r["error"]]
        queries.finish_run(
            conn,
            run_id,
            calendar_ok=results.get("calendar", {}).get("ok"),
            classroom_ok=results.get("classroom", {}).get("ok"),
            claude_calls_made=0,          # nothing calls Claude in Phase 0
            error="; ".join(errors) or None,
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily sync: Google Calendar + Classroom -> SQLite")
    parser.add_argument("--dry-run", action="store_true", help="fetch and parse, but write nothing")
    parser.add_argument("--verbose", action="store_true", help="debug-level logging")
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
    schema.init_db(conn)
    try:
        results = run_pipeline(conn, COLLECTORS, dry_run=args.dry_run)
    finally:
        conn.close()

    summary = ", ".join(
        f"{name} {'OK' if r['ok'] else 'FAILED'} ({r['rows']} rows)" for name, r in results.items()
    )
    all_ok = all(r["ok"] for r in results.values())
    log.info("run finished: %s", summary)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
