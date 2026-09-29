# Setup checklist

Everything you have to do by hand, in order. Each item says why it matters
and how to check it worked. Tick them off as you go.

Anything not listed here is already done and committed.

---

## 1. Sign in to Google again  — about once a week

**This is a recurring chore, not a one-off.** Google expires the sign-in
every 7 days for an app in *Testing* status, and publishing to production
is not available here: it needs a verified domain, which this project does
not have and does not warrant buying. Decided on 29 Sep - the alternative
was a domain purchase and a verification review for a personal tool.

Observed behaviour: signed in 20 Sep, expired 27 Sep. Exactly 7 days.

**When it happens, run:**

    python -m src.run_daily

A browser opens. Pick your account, click through the "unverified app"
warning (**Advanced -> Go to Life-Agent**), allow read-only access. Takes
about a minute.

**How you will find out:** the Notion page tells you, in red, at the top:

    Google needs you to sign in again (calendar). Until you do, this page
    will not update. Run: python -m src.run_daily

A second red warning appears whenever the data is more than six hours old
at the moment a run happens.

Note what that cannot cover: if the laptop is OFF, no run happens, so no
warning can be written. The page keeps whatever it last said. That is why
the page prints an actual timestamp -

    Google data: Tue 29 Sep 18:13

- rather than "just now". A relative age is only true at the instant of
writing and would freeze into a lie; a timestamp you compare against your
own clock cannot.

**If you ever get a Google Workspace account** (some universities issue
one), an app marked *Internal* under that organisation has no 7-day limit.
Not required - just the one thing that would remove this chore.

**Check it worked:**

    python -m src.run_daily --dry-run

Completes without opening a browser.

---

## 2. Install Ollama  — needed for step 4

**Why.** Step 4 runs a small model locally for routine work (tagging,
classifying), so the paid Claude API is only used once a day for the
synthesis. Free, and it never leaves your laptop.

1. Download from <https://ollama.com/download> - Windows installer
2. Run `OllamaSetup.exe`, accept defaults. It installs a background service
3. Open a **new** PowerShell window (existing ones will not see the command)
4. Pull the model - about 4.7 GB:

       ollama pull llama3.1:8b

5. Test it:

       ollama run llama3.1:8b "say hello in five words"

   Type `/bye` to exit.

Your machine has 31 GB RAM and an RTX 5060, so this runs on the GPU and
responds in seconds.

**Check it worked:**

    ollama list

Should show `llama3.1:8b`.

---

## 3. Anthropic API key  — needed for step 5, NOT before

Do this when step 5 starts, so you are not paying for an unused key.

1. Open <https://console.anthropic.com>
2. **API Keys → Create Key**, copy it
3. Paste into `.env`:

       ANTHROPIC_API_KEY=sk-ant-...

This is a **separate budget from Claude Pro** - the subscription does not
include API credits. V1 makes at most one call per run, on already-organised
data, so expect cents per month. The code refuses a second call per run.

---

## 4. Fill in your weekly rhythm  — optional, any time

Open `rhythm.toml`. Monday and Tuesday have themes taken from your own
notes; the rest are deliberately blank.

For each day you care about:

- `theme`    a short name, e.g. `"coursework"` or `"rest"`
- `focus`    one line in your own words, shown as-is on the Notion page
- `keywords` 3-6 words that appear in relevant titles or course names

A day with no theme is fine and fully supported - you still get everything
scheduled and everything due.

**Check it worked:**

    python -m src.rhythm.config

Reports any typo and shows the week. It refuses to run on a file with a
mistake rather than silently ignoring the line.

---

## Everyday commands

    python -m src.run_daily              sync, plan, write to Notion
    python -m src.run_daily --dry-run    same, but writes nothing anywhere
    python -m src.rhythm.today           sync and show today in the terminal
    python -m src.notion.writer --check  is Notion reachable and safe to write
    python -m pytest                     run the tests

Logs from every run are in `logs/agent.log`.
