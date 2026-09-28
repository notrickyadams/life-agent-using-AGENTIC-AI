# Setup checklist

Everything you have to do by hand, in order. Each item says why it matters
and how to check it worked. Tick them off as you go.

Anything not listed here is already done and committed.

---

## 1. Publish the Google app  — needed before step 6 (scheduler)

**Why.** While the app is in *Testing*, Google expires your sign-in every 7
days. This has already happened once (Calendar died on 27 Sep, exactly 7
days after sign-in). Overnight runs are impossible until it is fixed.

1. Open <https://console.cloud.google.com>
2. Select the **Life-Agent** project in the dropdown at the top
3. Left menu: **APIs & Services → OAuth consent screen**
   (newer consoles: **Google Auth Platform → Audience**)
4. Find **Publishing status: Testing**
5. Click **PUBLISH APP**, then **CONFIRM**
6. Status should read **In production**

If it offers a verification form, close it. Verification is only for
distributing the app to other people; publishing works without it. You will
still see an "unverified app" warning when signing in - click
**Advanced → Go to Life-Agent**. That is expected for a personal app.

**Check it worked:**

    python -m src.run_daily --dry-run

Should complete without opening a browser.

---

## 2. Re-sign in to Google Calendar  — after step 1

The expired token has to be replaced once.

    python -m src.collectors.calendar --list-calendars

A browser opens. Pick your account, click through the unverified warning,
allow read-only calendar access. It saves a token and will not ask again.

**Check it worked:** the command prints your calendars, including the
`classroom...` ones.

---

## 3. Install Ollama  — needed for step 4

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

## 4. Anthropic API key  — needed for step 5, NOT before

Do this when step 5 starts, so you are not paying for an unused key.

1. Open <https://console.anthropic.com>
2. **API Keys → Create Key**, copy it
3. Paste into `.env`:

       ANTHROPIC_API_KEY=sk-ant-...

This is a **separate budget from Claude Pro** - the subscription does not
include API credits. V1 makes at most one call per run, on already-organised
data, so expect cents per month. The code refuses a second call per run.

---

## 5. Fill in your weekly rhythm  — optional, any time

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
