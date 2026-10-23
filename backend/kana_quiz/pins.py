"""Per-request pins: the clock, the random source and the model's answers.

Replaying one request against two backends needs both to see the same
time, draw the same random numbers and read the same model text. Each of
those is a context variable set by the middleware in ``main`` and read
here. Without a pin ``now`` is the wall clock, ``new_rng`` is unseeded and
``ask_model`` calls the API.

The pins come from request headers when ``KANA_PINS=1``:

* ``X-Kana-Now`` an ISO 8601 instant.
* ``X-Kana-Seed`` an integer seed; every ``new_rng`` in the request starts
  the same stream from it.
* ``X-Kana-Model`` a JSON list of model answers, consumed in call order.

When ``KANA_RECORD`` names a file the middleware assigns a time and a seed
to every request that carries none, records the model's answers as they
arrive, and appends one JSON line per request with the request, the pins
and the response.
"""

from __future__ import annotations

import base64
import json
import random
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

_NOW: ContextVar[datetime | None] = ContextVar("kana_now", default=None)
_SEED: ContextVar[int | None] = ContextVar("kana_seed", default=None)
_SCRIPT: ContextVar[list[dict] | None] = ContextVar("kana_script", default=None)
_TRACE: ContextVar[list[dict] | None] = ContextVar("kana_trace", default=None)


@dataclass
class Pins:
    now: datetime | None = None
    seed: int | None = None
    # Model answers to play back, in call order. ``None`` lets the request
    # call the live API.
    script: list[dict] | None = None
    # Model calls the request made, in call order. The recorder reads it.
    trace: list[dict] = field(default_factory=list)


def install(pins: Pins) -> None:
    """Set the pins for the current context."""
    _NOW.set(pins.now)
    _SEED.set(pins.seed)
    _SCRIPT.set(pins.script)
    _TRACE.set(pins.trace)


def now() -> datetime:
    return _NOW.get() or datetime.now(timezone.utc)


def sql_now() -> str:
    """The pinned instant in the form SQLite's datetime('now') writes."""
    return now().strftime("%Y-%m-%d %H:%M:%S")


def new_rng() -> random.Random:
    seed = _SEED.get()
    return random.Random(seed) if seed is not None else random.Random()


class ModelScriptExhausted(RuntimeError):
    """The request asked the model more often than the script answers."""


def ask_model(name: str, model: str, call: Callable[[], str]) -> str:
    """Return the model's text for one call.

    ``name`` labels the call site. A scripted answer is ``{"name", "text"}``
    or ``{"name", "error"}``; an error entry raises ``GeminiUnavailable``
    with that message, which is what a refused live call raises.
    """
    from kana_quiz.gemini import GeminiUnavailable

    script = _SCRIPT.get()
    trace = _TRACE.get()
    if script is not None:
        if not script:
            raise ModelScriptExhausted(f"no scripted answer for {name}")
        answer = script.pop(0)
        if answer.get("name") != name:
            raise ModelScriptExhausted(f"script answers {answer.get('name')!r}, {name!r} asked")
        if "error" in answer:
            raise GeminiUnavailable(answer["error"])
        return answer["text"]
    try:
        text = call()
    except GeminiUnavailable as exc:
        if trace is not None:
            trace.append({"name": name, "model": model, "error": str(exc)})
        raise
    if trace is not None:
        trace.append({"name": name, "model": model, "text": text})
    return text


def from_headers(headers: Mapping[str, str]) -> Pins:
    """Read the pins a request carries. A missing header leaves its pin unset."""
    now_text = headers.get("x-kana-now")
    seed_text = headers.get("x-kana-seed")
    script_text = headers.get("x-kana-model")
    return Pins(
        now=datetime.fromisoformat(now_text) if now_text else None,
        seed=int(seed_text) if seed_text else None,
        script=json.loads(script_text) if script_text else None,
    )


def _text_or_b64(raw: bytes) -> dict:
    try:
        return {"text": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(raw).decode("ascii")}


def record(
    path: Path,
    pins: Pins,
    *,
    method: str,
    target: str,
    content_type: str | None,
    body: bytes,
    status: int,
    response: bytes,
) -> None:
    """Append one request, its pins and its response as a JSON line."""
    entry = {
        "method": method,
        "target": target,
        "content_type": content_type,
        "body": _text_or_b64(body) if body else None,
        "now": pins.now.isoformat() if pins.now else None,
        "seed": pins.seed,
        "model": pins.trace,
        "status": status,
        "response": _text_or_b64(response) if response else None,
    }
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")
