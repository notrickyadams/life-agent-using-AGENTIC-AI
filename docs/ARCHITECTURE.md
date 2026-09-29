# Architecture — V1

## Pipeline

```mermaid
flowchart TD
    SCHED[Scheduler<br/>daily, fixed time] --> COL[Collectors<br/>Calendar + Classroom sync]
    COL --> DB[(Local database<br/>SQLite)]
    DB --> LOOP{Agent loop<br/>routine or complex?}
    RHYTHM[Rhythm config<br/>weekly themes] --> LOOP
    LOOP -->|routine, free| LOCAL[Local model<br/>Ollama]
    LOOP -->|synthesis, rare| CLAUDE[Claude API<br/>paid]
    LOCAL --> NOTION[Notion<br/>daily view]
    CLAUDE --> NOTION
```

## Components

### Scheduler
OS-native (Task Scheduler on Windows, `launchd` on macOS, cron on Linux).
Runs `run_daily.py` once per day. No hosted infrastructure. If the laptop was
off at the scheduled time, the run happens the next time it's on — the
pipeline should be safe to run late or to run twice (idempotent writes).

### Collectors
Two independent functions, each wrapping one Google API:
- `sync_calendar()` — pulls events via Calendar API, upserts into SQLite
- `sync_classroom()` — pulls assignments/exams via Classroom API, upserts
  into SQLite

Each collector authenticates independently (separate OAuth scopes), handles
its own failures without crashing the other, and only ever adds/updates rows
— it never decides what's "important," that's the agent loop's job.

### Local database (SQLite)
Single file, single source of truth. Starting schema (extend as needed, don't
redesign):

- `events` — id, source (calendar/classroom), title, type
  (event/assignment/exam), due_at, raw_json, last_synced_at
- `tags` — event_id, tag, source (local_model/claude/manual)
- ~~`rhythm_days`~~ — **not built.** This line conflicted with the Rhythm
  config section below, which says the rhythm is "a plain, human-edited file
  (YAML or JSON, not a database table)". Resolved in favour of the file:
  `rhythm.toml` is the single source of truth, read fresh each run. Nothing
  needs to query a past rhythm, and two copies could disagree.
- `runs` — run_id, started_at, finished_at, claude_calls_made (for cost
  tracking — should always be 0 or 1 per run in V1)

### Rhythm config
A plain, human-edited file (not a database table). Built as **TOML**
(`rhythm.toml` in the repo root) rather than the YAML sketched here: Python
3.11+ parses TOML with the standard library, so it adds no dependency, and
it supports comments so the file can explain itself. The field named
`priority_tags` below is built as `keywords`, which is what it does — items
whose title or course name contains one are pulled into the day's section.

```toml
[days.monday]
theme = "certification_study"
focus = ""                       # your words, shown as-is
keywords = ["cca-f", "exam"]
```

A day with no theme is a supported state, not a placeholder: you still get
everything scheduled and everything due, just with no themed section. This
is what lets the rhythm be filled in gradually.

The agent loop reads this before touching the database so "what matters
today" is filtered through the day's theme from the start, not decided ad hoc
by a model each run.

### Agent loop (the router)
The one piece of actual "agent" logic in V1. For each item that needs
processing:

```
if task is routine (tag, dedupe, classify, filter-by-theme):
    call local model (Ollama)
else if task is the daily synthesis ("what matters today, in what order, why"):
    call Claude API — once per run, on already-organized data
```

This is the component most worth testing in isolation before wiring in real
data — feed it fake items and confirm routine tasks never reach the Claude
API call.

### Notion writer
Write-only in V1, into a **page** (not a database): one page rewritten each
run, so you open one bookmark and today is there. No reads from Notion feed
back into the pipeline — that's a later-phase feature if I start editing
things in Notion the agent should notice.

**How it avoids destroying anything.** The writer records the id of every
block it creates in the `notion_blocks` table, and a later run deletes
exactly those ids. A note, sub-page or database you add to that page is
never touched, because it was never on the list. It refuses outright to
write to a page holding content it has no record of creating, so a mistyped
page id fails loudly instead of quietly emptying a real page.

New blocks are appended *before* the old ones are deleted: if the append
fails the page is still intact, whereas deleting first would risk emptying
it. The marker callout at the top is only a label — an earlier version used
its position as the safety check, which locked the agent out of its own page
as soon as anything was added below it, since appends land at the end.

Items render as bullets rather than to-dos on purpose: a checkbox invites a
click that would do nothing, because V1 never reads Notion back and the tick
would be wiped on the next run.

## Orchestration: what kind of agent this is

V1 is a **pipeline with one routing decision**, not a multi-agent system.
`run_daily.py` is the orchestrator and it is **plain code, not a model** —
it calls each stage in a fixed order, isolating failures.

The router in the agent loop is an `if/else` on task type, not a model being
asked which tool to use. Routing therefore costs nothing and cannot
surprise you.

**Why not hub-and-spoke** (a central LLM decomposing work to LLM workers):
that earns its keep when the decomposition cannot be predicted in advance.
Here it can — "sync, filter by today's rhythm, write to Notion" is the same
sequence every day. An orchestrator model would add cost (a call per
decision, breaking the one-paid-call-a-day rule), make failures much harder
to locate, and introduce non-determinism into questions like "what is due
Thursday", which have exactly one right answer.

**Where it would earn its place: Phase 3+.** Opportunity discovery has
genuinely unpredictable decomposition — which sources to search next depends
on what the last search returned. That is the signal an orchestrator is
warranted. Even then the workers should be local or deterministic wherever
possible, with the expensive model reserved for judgement.

**Three lines not to cross**, in order of cost:

1. The model never decides which API to call. Collectors are dumb by design.
2. The model never loops until satisfied. Unbounded loops are unbounded spend.
3. Paid calls happen once, on already-organised data. Enforced in code:
   `record_claude_call()` refuses a second call in a run, and
   `runs.claude_calls_made` is counted rather than asserted.

| Pattern | Used in V1 | Where |
|---|---|---|
| Pipeline / workflow | yes | every stage |
| Router | yes | agent loop, as `if/else` |
| Parallelisation | no | possible in Phase 3 discovery |
| Hub-and-spoke | no | Phase 3+, if decomposition becomes unpredictable |
| Autonomous loop | no | not planned — no bounded task needs one |

## Synthesis: which model, and why it is a setting

CLAUDE.md specifies step 5 as one Claude API call per day. It is built as a
**setting** instead - `SYNTHESIS_MODEL` in `.env`, one of `ollama`,
`claude`, or `none` - defaulting to the free local model. Decided 29 Sep, on
evidence rather than preference, and it deviates in the cheaper direction:
the paid path is still there, one call per run, enforced by
`record_claude_call()` whichever backend is selected.

What the evidence showed, on a deliberately busy test day:

- Asked to summarise a day with an overdue lab report, llama3.1:8b wrote a
  fluent answer that never mentioned it. With a system prompt defining each
  section, it led with the overdue item correctly.
- Shown a lecture at 09:30-11:00 and a lab at 10:30-13:30, it reported "no
  conflict". The overlap is thirty minutes and plainly visible in the text.

So the local model is usable and it misses things. Worth knowing before
paying, and worth re-testing rather than assuming.

**Why prose needs a different guardrail from classification.** Tagging is
safe because the vocabulary is closed - anything unrecognised is discarded,
so the worst case is no tag. A paragraph has nothing to check against, and a
summary that quietly drops your overdue work reads exactly like a correct
one. One specific failure is therefore checked: if the plan has overdue
items and the summary names none of them, it is marked incomplete rather
than shown as if it were fine. That does not make prose verifiable. It
catches the mistake actually observed.

**End times exist for this step.** Items render as `09:30-11:00`, not
`09:30`, because the system prompt asks about clashes - and asking a model
about something the text does not contain is how invention starts.

## Trust / cost boundary

Everything above the Notion writer runs locally and costs nothing except the
one daily Claude API call. This boundary is the main thing to protect as the
system grows — see CLAUDE.md's non-negotiable constraints.

## Known limits and deliberate deviations

Recorded here so they are not rediscovered as surprises.

- **Calendar/Classroom duplicate detection is now proven on real data.**
  It decodes the Classroom link Google puts in a mirrored event's
  description. First real firing: 29 Sep 2026, on a Systems assignment due
  8 Oct, where the calendar mirror was correctly suppressed in favour of the
  Classroom row carrying the submission state.
- **Dedupe arrived in step 1, not step 4.** CLAUDE.md lists dedupe as
  local-model work. What was built is not that: it is an exact ID match
  decoded from a link, with no guessing and no model. The model's dedupe job
  in step 4 (same work described differently in two places) is untouched.
- **Classroom calendars are found by the id prefix `classroom`.** Every
  course calendar in this account follows it, but Google does not document
  it as guaranteed. If it stops matching, list the ids in `CALENDAR_IDS`
  and set `CALENDAR_INCLUDE_CLASSROOM=false`.
- **`rhythm.toml` holds the timezone, not Google.** The file is explicit,
  versioned and works offline. Each sync compares it with the account's
  setting and warns on a mismatch, so drift is detected rather than assumed
  away.
- **An unattended run never opens a browser.** `--no-browser` makes a
  needed sign-in a recorded failure instead of a process that waits
  forever. While the Google Cloud app is in *Testing* status, tokens expire
  about weekly, so this will fire until the app is published.

## What's explicitly out of scope for V1

- Web search / opportunity discovery
- Gmail or any outbound messaging
- Application tracking, drafting, or autofill
- Any permission-gated "pending approval" queue (nothing needs one yet,
  since nothing outbound exists in V1)
- Multi-skill harness structure (V1 is one loop, not separate named skills)

These are real, and covered in `ROADMAP.md`, but building them before the V1
spine is proven would mean debugging everything at once.
