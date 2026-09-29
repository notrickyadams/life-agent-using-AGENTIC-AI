"""
Works out what KIND of thing each item is, using the local model.

Why this exists: your Classroom titles are things like "Assignment 1",
"Assignment 6", "out of 60". Keyword rules cannot classify those - there is
nothing in the title to match. The description usually says what it really
is ("submit your lab report", "quiz covering chapters 3-4"), and reading a
sentence to decide "this is a lab" is exactly what a small local model is
good at. It runs on this laptop and costs nothing, so it can look at every
item.

THE RULE THAT MATTERS
    The model is given a CLOSED list of allowed tags and anything it
    returns that is not on that list is thrown away. A model that invents
    "urgent-ish", or answers with a sentence, or hallucinates a course
    name, produces no tag at all.

    That asymmetry is deliberate: a missing tag costs you a slightly less
    organised day. A wrong tag - "exam" on an ordinary homework - would
    quietly distort what the page tells you to do, and you would have no
    reason to doubt it.

    So: ambiguous answer -> no tag. Unrecognised answer -> no tag.

Tags are written to the tags table under source 'local_model', alongside
the deterministic 'rule' tags, never replacing them.
"""

import logging
import re

from src.agent import local_model

log = logging.getLogger(__name__)

# The complete vocabulary. Nothing outside this is ever stored.
# Kept small on purpose: every extra option is another way for a small model
# to be confidently wrong, and these are the distinctions that actually
# change how a day should be ordered.
ALLOWED_TAGS: dict[str, str] = {
    "exam": "a test, midterm, or final that must be sat",
    "quiz": "a short assessment, usually in class",
    "lab": "practical or laboratory work, often with a report",
    "project": "a larger piece of work spanning weeks",
    "assignment": "ordinary homework, a problem set, or an exercise",
    "reading": "material to read, with nothing to hand in",
    "presentation": "something to present or a set of slides",
    "admin": "registration, forms, announcements - no academic work",
}

# Where tags from this module are recorded. Scoped so rewriting them never
# disturbs the keyword-based 'rule' tags or anything set by hand.
MODEL_TAG_SOURCE = "local_model"

# The longest answer worth reading. A correct reply is one word.
MAX_ANSWER_CHARS = 200

# Past this many words, a reply is not an answer - it is the model
# explaining itself or echoing the question back. See parse_tag.
MAX_ANSWER_WORDS = 4

SYSTEM_PROMPT = (
    "You label university coursework. You answer with exactly one word from "
    "the list you are given, and nothing else. No explanation, no punctuation, "
    "no sentence. If none of the words fit, answer: none"
)


def describe_item(item) -> str:
    """The text the model gets to judge one item by.

    Title, course, and the description Google stored - the description is
    the part that actually distinguishes "Assignment 1" the lab report from
    "Assignment 1" the problem set.
    """
    parts = [f"Title: {item.title}"]
    if item.course_name:
        parts.append(f"Course: {item.course_name}")

    if item.description:
        parts.append(f"Description: {item.description}")
    return "\n".join(parts)


def build_prompt(item) -> str:
    """The full prompt for one item."""
    options = "\n".join(f"- {tag}: {meaning}" for tag, meaning in ALLOWED_TAGS.items())
    return (
        f"{describe_item(item)}\n\n"
        f"Which single word best describes this?\n{options}\n"
        "- none: if nothing above fits\n\n"
        "Answer with one word only:"
    )


def parse_tag(answer: str) -> str | None:
    """Turn whatever the model said into one allowed tag, or None.

    Strict by design. In order:
      1. The answer, cleaned of punctuation and case, IS an allowed tag.
      2. Otherwise, if the answer mentions exactly ONE allowed tag, take it -
         this rescues "Answer: lab" without accepting a sentence that
         weighs up several options.
      3. Anything else, including a reply mentioning two or more tags,
         yields None. Ambiguity is not resolved by guessing.
    """
    if not answer:
        return None

    text = answer.strip()[:MAX_ANSWER_CHARS].lower()

    # 1. exactly the word, allowing for trailing punctuation or markdown
    cleaned = text.strip().strip(".,:;!*_`\"'()[]{} \n\t")
    if cleaned in ALLOWED_TAGS:
        return cleaned
    if cleaned == "none":
        return None

    # 2. a short reply mentioning exactly one allowed tag.
    #    Short matters: "Answer: lab" is a padded answer, while "Assignment 1
    #    for Computer Networks" is the model echoing the title back, which
    #    happens to contain a tag word. Reading a tag out of an echo would
    #    label items by their own titles - precisely the thing the model was
    #    brought in to get past.
    if len(text.split()) <= MAX_ANSWER_WORDS:
        found = {tag for tag in ALLOWED_TAGS if re.search(rf"\b{re.escape(tag)}\b", text)}
        if len(found) == 1:
            return found.pop()

    # 3. nothing usable
    log.debug("discarding unrecognised answer %r", answer[:60])
    return None


def classify(item) -> str | None:
    """Ask the local model what kind of thing one item is.

    Returns an allowed tag, or None when the model is unsure, unhelpful, or
    unavailable. Never raises: one item that cannot be classified must not
    stop the rest, and no tag is always an acceptable answer.
    """
    try:
        answer = local_model.generate(
            build_prompt(item),
            system=SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=8,          # a correct answer is one word
        )
    except local_model.LocalModelError as exc:
        log.warning("could not classify %s: %s", item.id, exc)
        return None

    tag = parse_tag(answer)
    if tag is None and answer:
        log.debug("no tag for %s from answer %r", item.id, answer[:60])
    return tag


def classify_items(items: list) -> dict[str, str]:
    """Classify many items. Returns {item_id: tag} for those that got one.

    Runs one at a time. That is fine because the model is local and free:
    the first call of a run loads it (about a minute) and each one after
    takes about a second.
    """
    results: dict[str, str] = {}
    for item in items:
        tag = classify(item)
        if tag:
            results[item.id] = tag
    return results


def store_tags(conn, tags_by_id: dict[str, str]) -> int:
    """Write the model's tags, replacing only this source's previous ones.

    Cleared and rebuilt wholesale, like the rule tags: a tag is a function of
    the item and the current model, so a stale one from a previous run should
    not linger. Only MODEL_TAG_SOURCE is touched.
    """
    from src.db import queries

    queries.clear_tags_by_source(conn, MODEL_TAG_SOURCE)
    written = 0
    for item_id, tag in tags_by_id.items():
        written += queries.set_tags(conn, item_id, [tag], MODEL_TAG_SOURCE)
    conn.commit()
    return written
