# Roadmap — full project vision

V1 (see CLAUDE.md and ARCHITECTURE.md) is Phases 0–2 below. Nothing past
Phase 2 gets built until V1 is running reliably. This file exists so the
full ambition isn't lost, and so each later phase has a clear landing spot
instead of getting crammed into V1.

The full source document this roadmap is derived from lives at
`docs/VISION.md` (the original, unedited "2.0" prompt, sections 1-30). Each
phase below notes which VISION.md sections it corresponds to — check there
for the original wording and any detail this summary compressed.

## Phase 0 — Foundation
Google Calendar + Classroom OAuth and sync, SQLite schema, no AI.
**This is V1, step 1.**

## Phase 1 — Rhythm and local intelligence
Rhythm config file, local-model tagging/filtering via Ollama, no paid API
calls yet.
**This is V1, steps 2 and 4.**

## Phase 2 — Daily synthesis and delivery
Agent loop router, one daily Claude API call for synthesis, Notion write.
**This is V1, steps 3, 5, and 6. Once this phase works end-to-end, V1 is
done.**

---

## Phase 3 — Opportunity discovery engine
*(VISION.md sections 1, 5, 6)*
Web search across company pages, university sites, research labs,
conferences, scholarship portals — not just famous platforms. Tag each find
as NEW / ACTIVE / UPCOMING / CLOSED / RECURRING. Verify against primary
sources before treating anything as confirmed. Never invent deadlines,
eligibility, or funding — label anything unverified as such explicitly.

## Phase 4 — Application command center
*(VISION.md sections 7, 8, 9)*
Per-opportunity checklist (eligibility, deadline, CV, essays, references,
documents) with status tracking: Not Started → Preparing → Ready →
Submitted → Accepted/Rejected/Waitlisted. Application writing assistance
that draws only on real, verified achievements — no fabrication, no
exaggeration. Application autofill assistance, with a hard rule: nothing is
ever submitted without my explicit confirmation, and every submission gets
a final checklist review first.

## Phase 5 — University transfer intelligence & the opportunity map
*(VISION.md sections 10, 11, 12)*
Structured research on transfer requirements, credits, GPA thresholds,
funding, visa implications — always separated into FACT / INTERPRETATION /
UNKNOWN, never "you will get in." Opportunities organized into strategic
tiers (trajectory changers / compounders / exploration / monitor) rather
than a flat list. Weekly/monthly/semester/year action planning
(WHY → WHAT → HOW → DEADLINE → NEXT STEP for each action).

## Phase 6 — Networking, skill gaps, and project generation
*(VISION.md sections 16, 17, 18)*
Drafts for cold outreach (professors, researchers, founders) — short,
specific, evidence-based, never spammy. Skill-gap engine that turns "I'm
missing X" into a concrete project plan (current → gap → plan) instead of
just flagging the gap. Project suggestions that double as portfolio
evidence, not toy exercises.

## Phase 7 — Reflection loops
*(VISION.md sections 23, 24, 25, 26)*
Application autopsy after rejections (what to change, not "you're not good
enough"). Opportunity graph thinking — chains like competition → project →
portfolio → research contact → paper → transfer application, not isolated
wins. Anti-FOMO and anti-procrastination protocols: forcing evaluation
before "apply to everything," and breaking stalled opportunities into the
smallest next concrete action.

## Phase 8 — Full second brain
*(VISION.md sections 14, 15, 22)*
Complete Notion schema covering every opportunity field from the original
spec (funding, eligibility, required documents, strategic category, source,
last verified, etc.), full personal profile tracking (education, technical
skills, research, achievements, international exposure, application
assets), and the full daily/weekly briefing format with prioritized
sections (Act Now / Research / Career / International / University /
Competitions / Events / Profile Building).

---

## Ground rules that apply at every phase

- Primary sources preferred over aggregators; verify before presenting as
  fact.
- Never fabricate programs, scholarships, deadlines, people, or
  partnerships. Label anything unverified.
- No opportunity is ranked purely on volume of applications submitted —
  every phase optimizes for trajectory-relevant, compounding value.
- The Claude-API-usage discipline from V1 (local model for routine,
  API only for genuine synthesis) applies to every new phase too — a new
  feature that would call the API per-item needs a redesign before it ships.
