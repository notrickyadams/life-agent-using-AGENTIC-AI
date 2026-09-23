# CLAUDE.md — Personal Opportunity Agent ("2.0")

This file is read automatically by Claude Code at the start of every session in this
repo. It is the source of truth for scope, constraints, and working agreement.
Full architecture: `docs/ARCHITECTURE.md`. Full multi-phase vision: `docs/ROADMAP.md`.

## What this project is

A personal agent that runs on my laptop, pulls my Google Calendar and Google
Classroom data, filters it through a weekly "rhythm" (Monday = certification
study, Tuesday = open source, etc.), and pushes an organized daily view into
Notion — so I open Notion and see what to do, with no planning required from me.

Longer-term (see ROADMAP.md, not in scope yet): opportunity discovery
(internships, research, scholarships, competitions), application tracking and
drafting, university transfer research, networking outreach. None of that gets
built until the V1 spine below is working and trustworthy.

## CURRENT PHASE: V1 MVP — build only this

Do not build anything from ROADMAP.md phases 3+ unless I explicitly say so.
Scope creep here is the main risk to this project, more than any technical risk.

V1 consists of exactly these pieces, in this order:

1. Google Calendar + Google Classroom OAuth and sync into SQLite (no AI at all)
2. Rhythm config (a plain file, not a database) + logic to filter "what's
   relevant today" by theme (no AI at all)
3. Notion API integration: push a test page, confirm the write path works
   (no AI at all)
4. Local model (Ollama) wired in for routine tagging/filtering/dedupe (first AI)
5. One Claude API call per day for the actual synthesis step — "what matters
   today, in what order, why" (first and only paid AI call in V1)
6. Wire 1–5 together behind a scheduler (OS task scheduler, not a hosted cron)

V1 is done when: I can leave my laptop alone overnight, and the next time I
open it and run the pipeline, Notion shows an accurate, theme-filtered view of
my day without me touching anything.

## Non-negotiable constraints

**Cost.** I'm on a Claude Pro subscription (chat only, no API credits included)
plus a small separate API budget. The Claude API must never be called inside a
loop over individual items. Routine work (tagging, dedupe, classification,
filtering by rhythm) always goes to the local model. Claude API is called at
most once per scheduled run, on the already-organized data, for the synthesis
step only. If a design would call Claude API more than once per day in V1,
stop and flag it to me instead of implementing it.

**No autonomous outbound actions.** This system may read from Calendar,
Classroom, and write to Notion automatically. It must never send an email,
submit a form, or take any action outside my own accounts without me
explicitly confirming that specific action first. There are no outbound
actions in V1's scope at all — this rule matters more once ROADMAP phases 3+
are built.

**Local-first.** Everything in V1 runs on my laptop. One SQLite file is the
single source of truth — no second database, no hosted service, no
always-on server.

**I'm a coding beginner.** Explain what code does in plain language as you
write it, not just after. Prefer straightforward, readable code over clever
abstractions. Small functions with docstrings over frameworks I'd have to
learn separately.

**Never invent values.** Notion database IDs, OAuth scopes, calendar IDs,
API field names — if you don't have the real value, ask me for it or show me
how to find it. Don't guess and move on.

## Cross-cutting rules (apply from Phase 3 onward — not in V1's scope yet)

These come from `docs/VISION.md` sections 2, 3, 4, 27, 28, 29 and govern
every later phase once opportunity discovery, evaluation, or presentation
is actually built. Not relevant to V1's collectors/rhythm/synthesis work,
but recorded here now so they aren't lost or re-derived later:

- Evaluate opportunities by compounding value across multiple dimensions
  (skill growth, signaling, network, access, international exposure,
  research potential, career potential, narrative, feasibility, effort) —
  never by raw application count.
- Never present a bare link. Every opportunity gets: what, why it matters,
  who's eligible, when, where, funding, requirements, fit, gaps,
  application strategy, next action, effort, priority.
- Never fabricate a program, deadline, professor, funding amount, or
  requirement. Label anything unverifiable as UNVERIFIED. If sources
  conflict, present both and explain the discrepancy.
- Prefer official sources in this order: university/program/company site >
  government/institutional source > official conference/competition site >
  official announcement > reputable secondary source > community
  discussion.

## Architecture summary

```
Scheduler (daily, fixed time)
   -> Collectors (Google Calendar API + Google Classroom API)
   -> Local database (SQLite — single file, single source of truth)
   -> Agent loop, informed by Rhythm config (day -> theme -> priorities)
        -> routine work -> Local model (Ollama, free)
        -> synthesis step -> Claude API (paid, once per run)
   -> Notion (write-only in V1, this is the surface I actually look at)
```

Full diagram and component-by-component detail: `docs/ARCHITECTURE.md`.

## Tech stack

- Python 3.11+
- `google-api-python-client` + `google-auth-oauthlib` — Calendar & Classroom
- `sqlite3` (standard library) — local database
- Ollama, running a small local model (e.g. Llama 3.1 8B) for routine tasks
- `anthropic` Python SDK — for the single daily synthesis call only
- `notion-client` — Notion API writes
- OS-native scheduler (Task Scheduler / launchd / cron) — not a hosted cron

## Repo structure (propose exact layout before creating files)

```
/src
  /collectors      -- calendar.py, classroom.py
  /db              -- schema.py, queries.py
  /rhythm          -- config loader + "what's relevant today" logic
  /agent           -- loop.py (the router), local_model.py, claude_client.py
  /notion          -- writer.py
  run_daily.py     -- entry point the scheduler calls
/docs              -- ARCHITECTURE.md, ROADMAP.md (already exist, don't overwrite)
/tests
.env.example       -- documents required secrets, no real values
.gitignore         -- must exclude .env, *.db, token files
CLAUDE.md
```

## Secrets and credentials

All API keys, OAuth client secrets, and tokens go in `.env`, which is
git-ignored. Never hardcode a credential in source. Provide a `.env.example`
with placeholder names only. If a credential is missing, stop and ask me for
it — don't fabricate a placeholder and continue as if it worked.

## Working agreement for this repo

- Before writing any code, state the plan for that specific piece and wait
  for my go-ahead — don't build multiple V1 steps in one pass.
- Build and verify one component at a time, in the order listed above.
  Don't start step N+1 until step N actually runs.
- Write a short test or a manual verification step for anything that talks
  to an external API (Calendar, Classroom, Notion) — these fail silently in
  ways that are easy to miss.
- If you notice the design pulling toward calling Claude API more than once
  per run, or taking any outbound action automatically, stop and raise it
  with me rather than proceeding.
- If something in this file conflicts with what I ask for in chat, point out
  the conflict rather than silently picking one.
