"""
Writes the short "what matters today" summary that sits at the top of the
day plan - the one step where a model is asked for judgement rather than a
label.

Two backends behind one function, chosen by SYNTHESIS_MODEL:

    ollama   free, local, private. Decent, and it misses things.
    claude   the Anthropic API. Better at judgement, a few cents a month.
    none     no summary. The plan by itself is still a plan.

Defaulting to ollama means the pipeline costs nothing until you decide the
paid one is worth it, with both outputs in front of you.

WHY THIS STEP NEEDS A GUARDRAIL
    Classification could be made safe by giving the model a closed list and
    throwing away anything else. Prose has no such list - there is nothing
    to check a paragraph against, and a fluent summary that quietly omits
    your overdue assignment reads exactly like a correct one.

    Observed, not hypothetical: asked to summarise a day with an overdue
    lab report, llama3.1:8b produced a sensible-sounding answer that never
    mentioned it.

    So one specific failure IS checked: if the plan has overdue work and
    the summary names none of it, the summary is marked incomplete rather
    than shown as though it were fine. That does not make prose verifiable.
    It catches the one mistake we have actually seen.

THE COST RULE
    At most one paid call per run, enforced in queries.record_claude_call()
    rather than trusted to this file. Switching backend cannot change that.
"""

import logging
import re
from dataclasses import dataclass

from src import config

log = logging.getLogger(__name__)

# Kept short on purpose. This sits above a list you can already read; its job
# is to say what to do first, not to describe the list back to you.
MAX_SENTENCES = 4

SYSTEM_PROMPT = (
    "You help a student decide what to do first today. You are given their day "
    "plan, already sorted into sections. The sections mean exactly this:\n"
    "  OVERDUE  - past its due date and still not done\n"
    "  TODAY    - happening or due today\n"
    "  DUE SOON - due within the next few days\n"
    "  LATER    - further off, not urgent\n"
    "Rules you must follow:\n"
    "  - If anything is OVERDUE, it comes first and you must say so.\n"
    "  - Never describe a LATER item as urgent or overdue.\n"
    "  - Mention only what the plan shows. Invent nothing, and never refer\n"
    "    to a section that is not present above.\n"
    "  - Point out clashes between times if there are any.\n"
    f"  - At most {MAX_SENTENCES} sentences. No preamble, no bullet points."
)


class SynthesisError(Exception):
    """Raised when a synthesis backend is misconfigured or unreachable."""


@dataclass
class Summary:
    """The result of one synthesis attempt."""

    text: str
    backend: str
    complete: bool = True       # False when a known omission was detected
    note: str = ""              # why it is incomplete, for the log and the page
    claude_calls: int = 0       # what this actually cost, counted not assumed

    @property
    def usable(self) -> bool:
        """True when there is something worth putting on the page."""
        return bool(self.text.strip())


def build_prompt(plan) -> str:
    """The prompt for one day's plan."""
    from src.rhythm.today import format_plan

    return (
        f"Today is {plan.on:%A %d %B %Y}.\n\n"
        f"{format_plan(plan)}\n\n"
        "What should I do first today, and why?"
    )


# Words that mean "this is past its deadline". Matched on WORD BOUNDARIES,
# not as substrings: "is late" appears inside "th-is late-r today", which
# made an ordinary sentence read as a claim that something was overdue.
_OVERDUE_WORDS = re.compile(r"\b(overdue|past due|late)\b", re.IGNORECASE)


def check_covers_overdue(text: str, plan) -> tuple[bool, str]:
    """Check the one omission we have actually seen a model make.

    Returns (complete, note). If the plan has overdue work, the summary has
    to refer to at least one of those items - by a distinctive word from its
    title or course, or by saying "overdue" or "late" outright. Anything
    less and the reader would never know something was missing.

    Deliberately forgiving: this is looking for a total omission, not
    grading the writing.
    """
    claims_overdue = bool(_OVERDUE_WORDS.search(text))

    if not plan.overdue:
        # The opposite failure, and the one actually seen: with nothing
        # overdue, llama3.1:8b wrote "the Systems assignment is overdue"
        # about work due nine days later. Inventing urgency is worse than
        # omitting it - it sends you to do the wrong thing first.
        if claims_overdue:
            return False, "the summary calls something overdue when nothing is"
        return True, ""
    if claims_overdue:
        return True, ""

    lowered = text.lower()
    for item in plan.overdue:
        words = [w for w in (item.title or "").lower().split() if len(w) > 3]
        words += [w for w in (item.course_name or "").lower().split() if len(w) > 3]
        if any(word in lowered for word in words):
            return True, ""

    titles = ", ".join(i.title for i in plan.overdue[:3])
    return False, f"the summary does not mention overdue work ({titles})"


# --- backends ---------------------------------------------------------------

def _synthesise_with_ollama(prompt: str) -> tuple[str, int]:
    """Ask the local model. Free, so the call count is always zero."""
    from src.agent import local_model

    if not local_model.is_available():
        raise SynthesisError(
            "Ollama is not running, so there is no summary. Start it, set "
            "SYNTHESIS_MODEL=claude, or set SYNTHESIS_MODEL=none."
        )
    try:
        return local_model.generate(prompt, system=SYSTEM_PROMPT, temperature=0.0,
                                    max_tokens=300), 0
    except local_model.LocalModelError as exc:
        raise SynthesisError(str(exc)) from exc


def _synthesise_with_claude(prompt: str, conn=None, run_id: int | None = None) -> tuple[str, int]:
    """Ask the Anthropic API. The one paid call in V1.

    Recorded through queries.record_claude_call BEFORE the request, which
    refuses a second call in the same run. Recording first matters: a call
    that fails still cost money and still used the budget for this run.
    """
    if not config.ANTHROPIC_API_KEY:
        raise SynthesisError(
            "SYNTHESIS_MODEL is 'claude' but ANTHROPIC_API_KEY is empty in .env."
        )
    try:
        import anthropic
    except ImportError as exc:
        raise SynthesisError(
            "The anthropic package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    if conn is not None and run_id is not None:
        from src.db import queries

        queries.record_claude_call(conn, run_id)      # raises past the limit

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - any API failure is the same to us
        raise SynthesisError(f"The Claude API call failed: {exc}") from exc

    text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    return text.strip(), 1


# --- the one entry point -----------------------------------------------------

def synthesise(plan, backend: str | None = None, conn=None, run_id: int | None = None) -> Summary:
    """Write the summary for one day plan.

    Raises SynthesisError when the chosen backend cannot run. The caller
    decides whether that matters - it should not, because the plan itself is
    already complete without a summary.
    """
    backend = (backend or config.SYNTHESIS_MODEL).lower()

    if backend == "none":
        return Summary(text="", backend="none")

    if plan.urgent_total == 0:
        # Nothing needs ordering. The summary exists to say what to do FIRST,
        # and with only distant work listed there is no first - the list
        # already says everything there is to say.
        #
        # Asked anyway, llama3.1:8b replied "you can start with the DUE SOON
        # section" when no such section was on the page. A model given
        # nothing to rank will invent something to rank.
        return Summary(text="", backend=backend, note="nothing needs prioritising today")

    prompt = build_prompt(plan)

    if backend == "ollama":
        text, calls = _synthesise_with_ollama(prompt)
    elif backend == "claude":
        text, calls = _synthesise_with_claude(prompt, conn, run_id)
    else:
        raise SynthesisError(
            f"SYNTHESIS_MODEL is {backend!r}; expected 'ollama', 'claude', or 'none'."
        )

    complete, note = check_covers_overdue(text, plan)
    if not complete:
        log.warning("synthesis (%s) looks incomplete: %s", backend, note)

    return Summary(text=text.strip(), backend=backend, complete=complete,
                   note=note, claude_calls=calls)
