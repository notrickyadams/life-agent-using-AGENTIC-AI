"""
The local model: Ollama, running on this laptop.

This is where routine work goes - tagging, classifying, deciding what kind
of thing an item is. It is free and nothing sent to it leaves the machine,
so it can be called once per item without thinking about cost. That is the
whole reason it exists: it keeps the paid Claude API down to one call a day
for the step that genuinely needs judgement.

Talks to Ollama over plain HTTP rather than through a client library. The
request is four lines and you can see exactly what is sent, which matters
more here than saving those lines.

SPEED, because it is surprising the first time:
    Loading the model into the GPU takes about a minute. Answering
    afterwards takes about a second. Ollama keeps a model loaded for a few
    minutes after use, so one daily run pays the load cost once and is then
    fast for every item. A first call that seems to hang is almost always
    the model loading, not a failure - hence the generous timeout.

Check it any time with:
    python -m src.agent.local_model --check
"""

import argparse
import json
import logging
import sys
import time

import httpx

from src import config

log = logging.getLogger(__name__)


class LocalModelError(Exception):
    """Raised when Ollama is unreachable, missing the model, or fails."""


def _url(path: str) -> str:
    return f"{config.OLLAMA_HOST}{path}"


def is_available(timeout: float = 5.0) -> bool:
    """True when Ollama is running and answering.

    Deliberately quick and quiet: callers use this to decide whether to skip
    the local-model stage, not to diagnose it.
    """
    try:
        response = httpx.get(_url("/api/tags"), timeout=timeout)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def list_models(timeout: float = 10.0) -> list[dict]:
    """Every model installed in Ollama."""
    try:
        response = httpx.get(_url("/api/tags"), timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LocalModelError(
            f"Cannot reach Ollama at {config.OLLAMA_HOST}. Is it running? "
            "Install from https://ollama.com/download, then: ollama pull " + config.OLLAMA_MODEL
        ) from exc
    return response.json().get("models", [])


def has_model(name: str | None = None) -> bool:
    """True when the configured model is installed.

    Ollama reports names with their tag, e.g. 'llama3.1:8b'. A name given
    without a tag is matched against the part before the colon, since
    'llama3.1' and 'llama3.1:latest' mean the same thing to a person.
    """
    name = name or config.OLLAMA_MODEL
    installed = [m.get("name", "") for m in list_models()]
    if name in installed:
        return True
    return any(m.split(":")[0] == name.split(":")[0] for m in installed)


def generate(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> str:
    """Send one prompt to the local model and return its text.

    temperature defaults to 0 because every use here is classification, and
    the same item should get the same answer on Monday as on Tuesday. A
    daily view that changes its mind about unchanged data would be worse
    than useless.

    Raises LocalModelError rather than returning something empty, so a
    caller can never mistake a failure for a confident blank answer.
    """
    payload: dict = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if system:
        payload["system"] = system
    if max_tokens is not None:
        payload["options"]["num_predict"] = max_tokens

    try:
        response = httpx.post(
            _url("/api/generate"),
            json=payload,
            timeout=timeout if timeout is not None else config.OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise LocalModelError(
            f"The local model did not answer within "
            f"{timeout or config.OLLAMA_TIMEOUT_SECONDS}s. The first call after a "
            "restart loads the model and can take a minute; if it keeps timing out, "
            "check Ollama is running."
        ) from exc
    except httpx.HTTPError as exc:
        raise LocalModelError(f"Ollama request failed: {exc}") from exc

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise LocalModelError(f"Ollama returned something that is not JSON: {response.text[:200]}") from exc

    if "error" in body:
        raise LocalModelError(f"Ollama reported an error: {body['error']}")

    return (body.get("response") or "").strip()


def loaded_models(timeout: float = 5.0) -> list[dict]:
    """Models currently held in memory, with how much is on the GPU."""
    try:
        response = httpx.get(_url("/api/ps"), timeout=timeout)
        response.raise_for_status()
        return response.json().get("models", [])
    except httpx.HTTPError:
        return []


def check() -> dict:
    """Run a real prompt end to end and report what happened.

    Returns a summary rather than printing, so tests and the CLI can both
    use it. Raises LocalModelError if the model cannot be used at all.
    """
    models = list_models()
    names = [m.get("name", "") for m in models]

    if not has_model():
        raise LocalModelError(
            f"Model {config.OLLAMA_MODEL!r} is not installed. Installed: {names or 'none'}.\n"
            f"  Run: ollama pull {config.OLLAMA_MODEL}"
        )

    started = time.monotonic()
    answer = generate("Reply with exactly one word: ready", max_tokens=10)
    elapsed = time.monotonic() - started

    on_gpu = None
    for loaded in loaded_models():
        if loaded.get("name", "").split(":")[0] == config.OLLAMA_MODEL.split(":")[0]:
            size, vram = loaded.get("size", 0), loaded.get("size_vram", 0)
            # Ollama reports how much of the model sits in video memory.
            # All of it means the GPU is doing the work, which is the
            # difference between one second per item and thirty.
            on_gpu = bool(size and vram >= size * 0.95)

    return {
        "host": config.OLLAMA_HOST,
        "model": config.OLLAMA_MODEL,
        "installed": names,
        "answer": answer,
        "seconds": round(elapsed, 1),
        "on_gpu": on_gpu,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Local model (Ollama) - connection check")
    parser.add_argument("--check", action="store_true", help="verify Ollama, the model, and speed")
    parser.add_argument("--ask", help="send one prompt and print the reply")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # httpx logs a line per request; keep the output readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        if args.ask:
            print(generate(args.ask))
            return 0

        if not args.check:
            parser.print_help()
            return 0

        print(f"Checking {config.OLLAMA_MODEL} at {config.OLLAMA_HOST} ...")
        print("(the first call of the day loads the model and can take a minute)")
        summary = check()

        print(f"\nhost     OK - {summary['host']}")
        print(f"model    OK - {summary['model']}")
        print(f"installed  {', '.join(summary['installed'])}")
        print(f"replied    {summary['answer'][:60]!r} in {summary['seconds']}s")
        if summary["on_gpu"] is True:
            print("hardware   running on the GPU")
        elif summary["on_gpu"] is False:
            print("hardware   running on the CPU - it will work, just slower")
        print("\nReady. No paid API was used.")
        return 0
    except LocalModelError as exc:
        print(f"\nNOT READY:\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(_main())
