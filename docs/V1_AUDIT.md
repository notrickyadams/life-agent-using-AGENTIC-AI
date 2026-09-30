# V1 audit and fix log

A full review of V1, done on 30 September 2026, and the fixes made since.
The goal: V1 working 100%, fixed one step at a time, each fix proven by a
test that failed before it and passes after.

**Progress:** 12 fixed · 2 partly fixed · 2 withdrawn (were wrong) · 1 deferred · 11 still to do
**Tests:** 383 passing

---

## Status by section

Severity: **High** = V1 can show wrong information or fail · **Medium** =
wrong in some situations, or risky · **Low** = cleanup.

### 1. Collectors (Google sync) - ✅ all fixed

| # | Problem | Severity | Status |
|---|---|---|---|
| 1.1 | `python -m src.rhythm.today` could hide items it had just saved: it marked rows inactive using the time *after* saving, so a save that crossed into a new second hid them (about 1 run in 200) | High | ✅ Fixed (step 2) |
| 1.2 | The collectors' own `--sync` only saved rows: deleted items stayed, no run was recorded, the "Google data:" time never moved | Medium | ✅ Fixed (step 2) |
| 1.3 | A browser sign-in during `--dry-run` still saved a token file | Low | ✅ Fixed (step 2) |

### 2. Plan (rhythm) - ❌ to do (step 6)

| # | Problem | Severity | Status |
|---|---|---|---|
| 2.1 | "Now" is read twice while building the plan, so a run that crosses midnight plans inconsistently (`today.py`) | Low | ❌ To do |
| 2.2 | The timezone is read from `rhythm.toml` in two places, one of which silently falls back to UTC (`config.py`) | Low | ❌ To do |
| 2.3 | Times are compared as `"HH:MM"` text - works only because of zero-padding (`today.py`) | Low | ❌ To do |

### 3. Tagging (Ollama) - ❌ to do (step 6)

| # | Problem | Severity | Status |
|---|---|---|---|
| 3.1 | `tagger.classify()` duplicates the loop in `classify_items()` and is used only by tests | Low | ❌ To do |

### 4. Summary (synthesis) - ✅ mostly fixed

| # | Problem | Severity | Status |
|---|---|---|---|
| 4.1 | A second Claude call on the same day was reported as FAILED, exited 1, and put "could not read summary" on the page - for the daily limit doing its job | High (Claude only) | ✅ Fixed (step 3) |
| 4.2 | ~~Default model ID `claude-sonnet-5` may not exist~~ | - | ↩️ Withdrawn - it is a valid model (Claude Sonnet 5) |
| 4.2b | The Claude request caps output at 400 tokens while Sonnet 5 thinks by default, which could starve the answer | Medium (Claude only) | ⏸ Deferred until Claude is switched on |
| 4.3 | The "incomplete summary" display can never run - rejected summaries are emptied first (dead code in `writer.py`, `today.py`) | Low | ❌ To do (step 6) |
| 4.4 | Called without a database connection, the Claude backend made a paid call without counting it (found during step 3) | Medium (Claude only) | ✅ Fixed (step 3) |

Claude is **not connected yet** (no API key, zero calls ever made). 4.1, 4.2b
and 4.4 only matter once `SYNTHESIS_MODEL=claude` is set.

### 5. Notion writer - ✅ mostly fixed

| # | Problem | Severity | Status |
|---|---|---|---|
| 5.1 | When Notion would not say whether a block had notes nested under it, the writer deleted it anyway - which could delete your note too | Medium | ✅ Fixed (step 4) - now kept for that run and retried |
| 5.2 | ~~No retry on rate limits or server errors~~ | - | ↩️ Withdrawn - notion-client 3.1.0 already retries (2 retries by default) |
| 5.3 | The page ID with and without dashes was treated as two different pages, so the writer refused its own page | Low | ✅ Fixed (step 4) |
| 5.4 | Text is cut to 2000 Python characters, but Notion counts UTF-16 units, so emoji-heavy text could still exceed the limit | Low | ❌ To do (step 6) |

### 6. Orchestration and scheduler

| # | Problem | Severity | Status |
|---|---|---|---|
| 6.1 | The 07:00 run uses `--if-stale 6`, so it is skipped if you synced in the last six hours - contradicting the script's own comment and SETUP.md | Medium | ❌ To do (step 5) - decided: split into two tasks |
| 6.2 | The Notion warning said "could not read *summary*" - data-source wording for a step that reads nothing | Medium | ✅ Fixed (step 3) |

### 7. Tests - ✅ mostly fixed

| # | Problem | Severity | Status |
|---|---|---|---|
| T1 | Tests wrote to the real `logs/agent.log` and needed the real `credentials.json` | Medium | ✅ Fixed (step 1) |
| T2 | Nothing stopped a future test making a real, paid Claude call | Medium | ✅ Fixed (step 1) - three locks, checked by `tests/test_isolation.py` |
| T3 | Tests depended on the real `rhythm.toml` | Medium | ✅ Fixed (step 1) - fixed copy in `tests/fixtures/` |
| T4 | Missing tests: `config.py`, and the bugs above | Low | 🟡 Partly - every fix above now has a test; `config.py` still has none |

### 8. Docs - ❌ to do (step 7)

| # | Problem | Status |
|---|---|---|
| D1 | `CLAUDE.md`: repo layout lists files that don't exist (`loop.py`, `claude_client.py`); says synthesis uses Claude (default is Ollama); says "per run" (limit is per day) | ❌ To do |
| D2 | "One paid call per run" wording | 🟡 Fixed in code (`config.py`, `run_daily.py`, `synthesis.py`); `SETUP.md` still says per run |
| D3 | `ARCHITECTURE.md` describes an old database schema and old function names | ❌ To do |
| D4 | `.env.example`: `ANTHROPIC_MODEL` missing; still says page-vs-database is undecided | ❌ To do |
| D5 | `SETUP.md`: Anthropic key only needed for `SYNTHESIS_MODEL=claude`; wrong step number; scheduler description | ❌ To do |
| D6 | `README.md` was saved as UTF-16 (displayed garbled) with only a title | ✅ Rewritten |

---

## Fix plan

| Step | What | Status |
|---|---|---|
| 1 | Make the tests safe: no real files, services, or paid calls (T1-T3) | ✅ Done |
| 2 | Sync correctness (1.1, 1.2, 1.3) | ✅ Done |
| 3 | The paid-call path (4.1, 4.4, 6.2) | ✅ Done |
| 4 | Notion writer safety (5.1, 5.3) | ✅ Done |
| 5 | Scheduler: two tasks, so the 07:00 run always syncs (6.1) | Next |
| 6 | Small cleanups (2.x, 3.1, 4.3, 5.4, T4) | To do |
| 7 | Docs and `CLAUDE.md` (D1-D5) | To do |
| 8 | End-to-end check on the real laptop: dry run, two real runs, page replaced not duplicated, scheduler run | To do |

Work happens on the `fix/v1-audit` branch, one commit per step.

---

## Corrections to the original audit

Two findings were wrong and have been withdrawn:

- **4.2** - `claude-sonnet-5` was flagged as a possibly invalid model ID. It is
  valid (Claude Sonnet 5; the current Sonnet is `claude-sonnet-5-5`). Checking
  it did surface a different real issue, logged as **4.2b**.
- **5.2** - The Notion client was said not to retry. notion-client 3.1.0
  retries rate limits on every request and server errors on reads and
  deletes, and the app leaves that on.

Lesson kept: check the installed library or official source before calling
something a bug.

---

## Open question: "things I add to Google Calendar don't appear"

Investigated on 30 September 2026. What was checked:

- Every calendar is synced: the main calendar and all 15 Classroom course
  calendars (only the two "Holidays in Egypt" calendars are skipped).
- Everything Google returns reaches the page: a fresh read returned 3
  calendar events, and all 3 were on the plan.
- The scheduled task is installed and succeeding, and every recent run read
  Google and wrote Notion successfully.

So the missing items are not being dropped by the agent - Google is not
returning them. Likely causes, still to be confirmed:

1. **It was a Google *Task*, not an *event*.** Tasks are a separate Google
   service that V1 does not read.
2. **The page had not refreshed yet** - it only updates when the agent runs.
3. **It is outside the 7-days-back / 30-days-ahead window.**
4. **It was added in a different Google account.**

Possible V1 improvements, **waiting on a decision**:

- Refresh every couple of hours instead of once a day (would fit into step 5;
  free, since Ollama is local).
- Read Google Tasks as a third source (new scope - needs explicit approval).
