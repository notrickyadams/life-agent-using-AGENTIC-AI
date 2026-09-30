# I Built an AI Agent That Plans My Day. The Hardest Part Was Stopping It From Lying.

### Building Life Agent, V1: Google Calendar and Classroom in, one honest Notion page out, and at most one paid AI call a day

---

Every morning I used to do the same small, draining job. Open Google Calendar. Open Google Classroom. Work out what was due, what clashed, and what I'd meant to spend the day on. Then decide where to start.

None of it is hard. It's just friction, and friction every morning adds up to a lot of wasted attention.

So I'm building an agent to do it for me. The long-term plan is big: an "opportunity agent" that finds internships, research programs, scholarships and competitions, helps with applications, and keeps it all organised in Notion. I'm documenting every version here as I go.

**This post covers V1.** It's the first working piece, and I kept it deliberately small.

---

## What V1 does

One sentence: **it reads my Google Calendar and Google Classroom, filters everything through my weekly rhythm, and writes a clean daily view into a Notion page.** It runs every morning before I wake up.

I open one bookmark and my day is there:

- A short summary of what to do first and why
- What's **overdue**
- What's on **today**
- What's **due soon**
- A **themed section** for what today is *for* (Monday is certification study, Tuesday is open source)
- Everything due **later**, so nothing I owe is ever hidden

My definition of done for V1 was one line:

> I can leave my laptop alone overnight, and the next time I open it, Notion shows an accurate, theme-filtered view of my day without me touching anything.

It now does that.

---

## The rules I set before writing any code

I'm still new to coding, and I knew my biggest risk wasn't technical. It was scope creep: trying to build the grand vision all at once and ending up with nothing that works. So before I started, I wrote down a few rules and put them in the repo.

**1. Build only V1.** Six steps, in order, and a step doesn't start until the one before it actually runs.

**2. Cost discipline.** Paid AI (the Claude API) may be called **at most once per run**, and never inside a loop over individual items. Routine work like tagging and classifying goes to a free local model.

**3. Local-first.** Everything runs on my laptop. One SQLite file is the single source of truth, with no hosted database and no always-on server.

**4. No outbound actions.** The agent may read my Google data and write to my own Notion page. It never sends anything anywhere.

**5. Never invent values.** No guessed IDs, field names or API scopes. If the real value isn't known, stop and ask.

That last rule kept coming back, in ways I didn't expect.

---

## The architecture: a pipeline, not a swarm of agents

When people say "AI agent" now, they often mean a big model that orchestrates other models, decides which tools to call, and loops until it's satisfied. I didn't build that, and choosing not to was one of the most useful decisions in the project.

```
Scheduler (Windows Task Scheduler, every morning)
   -> Collectors      Google Calendar + Google Classroom (read-only)
   -> SQLite          one file, the single source of truth
   -> Rhythm          a plain text file: what each weekday is for
   -> Tagging         local model (Ollama, llama3.1:8b), free
   -> Summary         one call per run, local by default, Claude optional
   -> Notion          the page I actually look at
```

The orchestrator is **plain Python, not a model.** It runs each stage in a fixed order, and a failure in one stage doesn't take down the others.

Why not have a big model in charge? Because my day's workflow is the same every day: sync, filter, summarise, write. A model deciding what to do next would add cost to every decision, make failures much harder to track down, and add randomness to questions like "what's due Thursday?", which have exactly one right answer.

I wrote down three lines the model never crosses:

1. **The model never decides which API to call.** The collectors are dumb on purpose.
2. **The model never loops until it's satisfied.** An unbounded loop means unbounded spend.
3. **Paid calls happen once, on data that's already organised.** This is enforced in code, not just promised: a function refuses a second paid call in the same run, and every run records how many it made.

A big orchestrating model may earn its place later, when the agent has to search the web for opportunities and can't know in advance where the next search leads. For V1 it would have been an expensive way to make things worse.

---

## How it was built, step by step

### Step 1: Pull the data (no AI)

Two collectors, one for Calendar and one for Classroom. Each signs in separately with **read-only** access and saves everything into SQLite.

Some details that mattered more than I expected:

- **Running twice is safe.** Every row has a stable ID, so a second run updates instead of duplicating.
- **Nothing is ever deleted.** If Google stops returning something, the row is marked inactive and kept.
- **No double-counting.** Classroom copies assignments into Calendar, so every assignment could have shown up twice. Instead of matching by title, which is a guess, the collector decodes the Classroom link Google puts inside the calendar event's description. That's an exact match, with no model involved.

### Step 2: The weekly rhythm (no AI)

The rhythm is a plain, commented text file (TOML) that I edit by hand:

```toml
[days.monday]
theme = "certification_study"
focus = ""
keywords = ["cca-f", "cert", "exam"]

[days.tuesday]
theme = "open_source"
keywords = ["oss", "open source", "github", "pull request"]
```

A day with no theme is fine. It still shows everything scheduled and due, just without a themed section, so I can fill in my week gradually.

The file is strict about typos. Early on, a misspelled header like `[day.monday]` was quietly accepted and defined nothing, which is exactly the silent failure validation exists to stop. Now the file rejects unknown sections and suggests what I probably meant.

### Step 3: Write to Notion (no AI)

This step made me more nervous than any AI part, because **the agent rewrites a real page every day.** If it ever deletes the wrong thing, I lose my own notes.

The approach: the agent records the ID of every block it creates, and the next run deletes **exactly those IDs and nothing else.** Anything I add to the page myself (a note, a sub-page, a database) is never touched, because it was never on the list. The agent also refuses to write to a page that holds content it has no record of creating, so a mistyped page ID fails loudly instead of quietly emptying a real page.

The order matters too. New blocks are added *before* old ones are deleted. If adding fails, the old page is still there. Deleting first could leave me with an empty page.

My first safety check was a "marker" block that had to sit at the top of the page. It passed every test. Then, **on its first run against the real page**, the agent locked itself out: Notion adds new blocks at the bottom, so after one rewrite my own notes sat above the marker and the agent refused to write again. No unit test caught it, and the live API showed it straight away. Recording block IDs replaced the marker, and the lockout became a regression test.

### Step 4: The first AI, a local model for tagging

Classroom titles are often useless: "Assignment 1", "out of 60". Keyword rules can't classify those. The description usually says what the thing really is, and reading a sentence to decide "this is a lab" is a good job for a small local model.

I run **llama3.1:8b through Ollama** on my own laptop, so it's free and private. In testing it got 8 out of 8 realistic cases right, including the one that justifies the whole step: two items **both titled "Assignment 1"**, one described as "submit your lab report" and the other as "solve problems 1 to 12." It tagged them correctly as *lab* and *assignment* from the descriptions alone.

The key design choice is a **closed vocabulary**: eight allowed tags. Anything the model returns that isn't on the list is thrown away. If the answer is ambiguous (two tags, a tag buried in a longer word, a negation like "not a lab"), the item gets no tag at all.

The reasoning: **a missing tag costs me a slightly messier day. A wrong tag quietly changes what the page tells me to do, and nothing would make me doubt it.**

The step is optional. If Ollama isn't running, I still get the full plan without labels, and the run isn't reported as broken.

### Step 5: The daily summary, where honesty got hard

This is the "what matters today, in what order, and why" paragraph at the top of the page.

My original plan said to use the Claude API. I built it as a **setting** instead: `ollama` (free, the default), `claude` (paid), or `none`. The one-paid-call-per-run limit still applies whichever I choose. Starting with the free model means I can decide whether to pay based on evidence, with both outputs side by side, instead of assuming up front.

The evidence was mixed:

- On a busy test day with an overdue lab report, the local model wrote a fluent summary **that never mentioned the overdue report.** A clearer system prompt fixed that.
- Given a lecture at 09:30–11:00 and a lab at 10:30–13:30, it said **"no conflict."** The overlap is thirty minutes and plainly in the text. It still gets this wrong.
- On a quiet day, I asked it to rank two items that were both weeks away. It told me to "start with the DUE SOON section", **a section that wasn't on the page.** Given nothing to rank, it invented something. So now the summary is skipped entirely unless something is overdue, due today, or due soon. With nothing urgent, there's no "first", and the list says everything already.

Tagging was easy to make safe because the vocabulary is closed. **A paragraph has nothing to check against.** A summary that quietly drops your overdue work reads exactly like a correct one.

So I check the specific failures I've actually seen:

1. **Omission.** If the plan has overdue items and the summary names none of them, the page marks the summary as incomplete, in yellow, and tells me to check the list.
2. **Invention.** This one came from the real page, the day V1 finished. I added a meeting for the next day and the model wrote: *"The Systems Assignment 1 is overdue."* It wasn't. It was due on 8 October and listed under LATER. My guardrail only checked one direction. **Invented urgency is worse than omission.** An omission leaves you to read the list yourself, but invented urgency sends you to do the wrong thing first, with a confident reason attached. Now a summary that claims something is overdue when nothing is gets flagged too.

The fix for that introduced its own bug, caught by the tests added with it: matching the phrase "is late" also matched "th**is late**r today." Now matching uses whole words only.

This doesn't make prose verifiable. It catches the mistakes I've actually observed, and I think that's the honest version of "AI guardrails" for now.

### Step 6: The scheduler, so the day is ready before I look

A Windows Task Scheduler job runs the pipeline every morning. Three settings each come from a problem I hit earlier:

- **It never opens a browser.** Google's sign-in expires weekly for personal projects like this (more below). A scheduled run that hit a sign-in prompt would wait forever. Instead it records the failure, and the Notion page says so in red.
- **If the laptop was off, it runs when it next wakes up**, instead of silently skipping a day.
- **No password is stored anywhere.**

For the evenings, there's also `refresh.bat`: one double-click to update the page. I asked myself what "refresh my day" actually meant, and the honest answer was: open a terminal, find the folder, remember a command. That's the kind of friction that quietly decides whether a tool gets used at all.

---

## The theme I didn't plan for: a stale page looks exactly like a current one

The lesson I'll carry into every future version is that **the page must never look more certain than it is.**

- Early versions said *"Google data: just now."* That was true only at the moment it was written. If my laptop is off for a week, no run happens to correct it, so the page keeps claiming it's fresh. Now it prints an actual timestamp, *"Google data: Tue 29 Sep 18:13"*, which I can check against my own clock. A timestamp can't turn into a lie over time.
- If the data is more than six hours old when a run happens, the page says so in red.
- If Google needs me to sign in again, the page says that in red at the top, with the exact command to run.
- One day I reported *"I can't see my assignments in Notion."* Nothing was broken: the only assignment was nine days out, beyond the "due soon" window, so the page said *"Nothing scheduled, nothing due."* Technically correct, and unhelpful. Now there's a **later** section, and the page is never falsely empty.

A falsely empty page, a frozen "just now", a confident invented deadline: each looks deliberate, so you'd have no reason to question it. That makes them worse than a crash.

---

## A constraint I accepted instead of fighting

Google expires sign-ins **every 7 days** for apps in "Testing" status. I measured it: signed in 20 September, expired 27 September. Removing the limit means publishing the app, which needs a verified domain. Buying one and going through a verification review isn't worth it for a personal tool.

So re-signing in once a week is a documented chore, not a bug. The design follows from that: the chore has to be impossible to miss, which is why the warning goes on the Notion page I read every day, not into a log file I never open.

---

## V1 by the numbers

- **6 steps**, built in order, each verified against real data before the next began
- **343 automated tests**, none of which need a network connection or a running model
- **0 paid API calls** so far. The free local model handles everything, and the paid path is there when the evidence justifies it
- **1 SQLite file**, **1 text file** for my rhythm, **1 Notion page**
- **About a week** from the first sync to the finished V1

## Tech stack

Python 3.11 · Google Calendar and Classroom APIs (read-only OAuth) · SQLite · Ollama running llama3.1:8b · Anthropic Claude API (optional, one call max per run) · Notion API · Windows Task Scheduler

I built this with Claude Code as a pair programmer. As a beginner I asked it to explain each piece in plain language as we wrote it, and I didn't let it start a step until I'd approved the plan for that step. Every commit message records what was found and why a decision was made. Rereading them to write this post was a good reminder of how many small mistakes were caught before they became habits.

---

## What I learned

1. **Scope is the real risk.** Writing down "build only this" was the most valuable file in the repo.
2. **Use a model only where rules can't do the job.** Deduplication turned out to be an exact ID match, not an AI task. Classifying "Assignment 1" from its description genuinely needed a model.
3. **Close the vocabulary whenever you can.** A model choosing from eight tags can be checked. A model writing freely can't.
4. **Test against the real thing early.** The Notion lockout, the invented "overdue" and the falsely empty page were all found on the live page, not in unit tests.
5. **Missing information is better than confidently wrong information.** No tag beats a wrong tag. No summary beats an invented ranking. A timestamp beats "just now."

---

## What's next

V1 is the foundation: a daily view I trust. Next I'll run it for real, every day, and see what breaks. After that the roadmap moves to the part that motivated the project: **finding opportunities**, meaning internships, research programs, scholarships and competitions, checked against official sources and never invented.

That's where a smarter orchestrator may finally earn its place. It's also where "never make things up" goes from a nice principle to the whole point.

See you in V2.

---

*If you're building something similar, or you've solved the "local model says there's no clash when there obviously is" problem, I'd love to hear from you in the comments.*

