"""Measure per-call grader cost across Gemini models.

For each (target word, student answer) case, build the same prompt the
production grader uses, call ``client.models.generate_content`` with
``response.usage_metadata`` enabled, and print a markdown table of
input / output / thinking tokens and the resulting dollar cost at
published list pricing.

The model list defaults to a spread from "what we ship today" down to
"what we could downgrade to" so the user can decide whether switching
is worth the verdict-quality tradeoff. Pass ``--models a,b,c`` to
override.

Pricing is embedded but explicitly dated — Google changes Gemini list
prices periodically. Re-check ai.google.dev/pricing before quoting
absolute numbers.

Usage:
    GEMINI_API_KEY=... uv run python scripts/grader_cost.py
    GEMINI_API_KEY=... uv run python scripts/grader_cost.py \
        --models gemini-3-flash-preview,gemini-2.5-flash-lite
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from kana_quiz import gemini  # noqa: E402
from kana_quiz.models import Word  # noqa: E402


# Published list pricing per 1M tokens (input / output), USD.
# Snapshot: 2026-05-10 from ai.google.dev/gemini-api/docs/pricing.
# Re-check before quoting absolute numbers — Google does revise the
# list. Thinking tokens bill at the OUTPUT rate.
#
# Prices listed are the ≤200k-prompt-length tier. Our grader prompt is
# ~1.3k tokens so we always hit that tier; the >200k tier is only
# relevant for Pro-with-huge-context use cases that this grader will
# never see.
PRICING: dict[str, tuple[float, float]] = {
    # Gemini 3.1 (preview)
    "gemini-3.1-pro-preview":          (2.00, 12.00),
    "gemini-3.1-flash-lite":           (0.25,  1.50),
    "gemini-3.1-flash-lite-preview":   (0.25,  1.50),
    # Gemini 3.0 (preview)
    "gemini-3-flash-preview":          (0.50,  3.00),
    # Gemini 2.5 GA
    "gemini-2.5-pro":                  (1.25, 10.00),
    "gemini-2.5-flash":                (0.30,  2.50),
    "gemini-2.5-flash-lite":           (0.10,  0.40),
    # Gemini 2.0 (deprecated tier on the price page, but still callable)
    "gemini-2.0-flash":                (0.10,  0.40),
    "gemini-2.0-flash-001":            (0.10,  0.40),
    "gemini-2.0-flash-lite":           (0.075, 0.30),
    "gemini-2.0-flash-lite-001":       (0.075, 0.30),
    # gemini-3-pro-preview isn't broken out on the docs pricing page
    # (only gemini-3-pro-image-preview is) — leave it out so the cost
    # column renders "?" for that model rather than a guess.
}


@dataclass(frozen=True)
class Case:
    label: str
    word: Word
    typed: str
    direction: str  # "ja2en" or "en2ja"
    expected: str = ""  # expected verdict; empty = "don't check"


def W(word_id: int, kana: str, kanji: str, english: str) -> Word:
    return Word(
        id=word_id, kana=kana, english=english,
        kanji=kanji or None, tags=(), deck_id=1,
    )


# A handful of representative miss patterns — short and clearly-wrong,
# longer with a synonym alternate, and an ambiguous-gloss case that
# triggers clarified_gloss (which lengthens the output). Average them
# and we get a realistic per-miss cost.
CASES: list[Case] = [
    Case("ja2en clean correct", W(99, "こんばん", "今晩", "tonight, this evening"),
         "evening", "ja2en", expected="correct"),
    Case("ja2en synonym",       W(4509, "やくだつ", "役立つ", "to be useful, to be helpful"),
         "handy", "ja2en", expected="correct"),
    Case("ja2en wrong",         W(3964, "じゅうよう", "重要", "important, essential"),
         "banana", "ja2en", expected="incorrect"),
    Case("en2ja ambiguous gloss", W(5205, "ほうそく", "法則", "law, rule, principle"),
         "ほうりつ", "en2ja", expected="accept"),
    Case("en2ja trans/intrans",  W(3861, "おきる", "起きる", "to wake up, to get up"),
         "おこす", "en2ja", expected="incorrect"),
    Case("en2ja IME artifact",   W(99, "こんばん", "今晩", "tonight, this evening"),
         "こんばn", "en2ja", expected="correct"),
]


@dataclass
class Sample:
    """One model response with full token accounting."""
    model: str
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int
    total_tokens: int
    latency_ms: float
    verdict: str | None
    error: str | None = None


def _build_prompt(case: Case) -> str:
    if case.direction == "ja2en":
        return gemini._GRADE_PROMPT_JA2EN.format(
            kana=case.word.kana,
            kanji=case.word.kanji or "(none)",
            english=case.word.english,
            typed=case.typed,
        )
    return gemini._GRADE_PROMPT_EN2JA.format(
        english=case.word.english,
        kana=case.word.kana,
        kanji=case.word.kanji or "(none)",
        typed=case.typed,
    )


def _grade_once(client, types_mod, model: str, case: Case) -> Sample:
    schema = types_mod.Schema(
        type=types_mod.Type.OBJECT,
        required=["verdict", "explanation", "alternates"],
        properties={
            "verdict": types_mod.Schema(
                type=types_mod.Type.STRING,
                enum=["correct", "accept", "incorrect"],
            ),
            "explanation": types_mod.Schema(type=types_mod.Type.STRING),
            "alternates": types_mod.Schema(
                type=types_mod.Type.ARRAY,
                items=types_mod.Schema(type=types_mod.Type.STRING),
            ),
            "clarified_gloss": types_mod.Schema(type=types_mod.Type.STRING),
        },
    )
    # Mirror production config so token counts stay representative.
    cfg_kwargs: dict = dict(
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0.0,
    )
    # Thinking is a Gemini 3.x feature; older models 400 if we send the
    # config at all. Gate on the model name so the comparison sweep
    # works across mixed eras of the API. "3.1-flash-lite" still falls
    # under the gemini-3 prefix so it picks up the thinking knob.
    #
    # Pro models in the gemini-3 line don't accept MINIMAL — they
    # require LOW or higher — so use LOW there. Flash variants accept
    # MINIMAL and that's what production ships with.
    if model.startswith("gemini-3"):
        try:
            level = (
                types_mod.ThinkingLevel.LOW
                if "pro" in model
                else types_mod.ThinkingLevel.MINIMAL
            )
            cfg_kwargs["thinking_config"] = types_mod.ThinkingConfig(thinking_level=level)
        except AttributeError:
            pass

    prompt = _build_prompt(case)
    t0 = time.perf_counter()
    try:
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types_mod.GenerateContentConfig(**cfg_kwargs),
        )
    except Exception as e:
        return Sample(
            model=model, prompt_tokens=0, output_tokens=0,
            thinking_tokens=0, total_tokens=0,
            latency_ms=(time.perf_counter() - t0) * 1000,
            verdict=None, error=f"{type(e).__name__}: {e}",
        )
    latency_ms = (time.perf_counter() - t0) * 1000
    usage = getattr(resp, "usage_metadata", None)
    verdict: str | None = None
    try:
        verdict = json.loads(resp.text or "{}").get("verdict")
    except Exception:
        verdict = None

    if usage is None:
        return Sample(
            model=model, prompt_tokens=0, output_tokens=0,
            thinking_tokens=0, total_tokens=0,
            latency_ms=latency_ms, verdict=verdict,
            error="response carried no usage_metadata",
        )
    return Sample(
        model=model,
        prompt_tokens=getattr(usage, "prompt_token_count", 0) or 0,
        output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
        thinking_tokens=getattr(usage, "thoughts_token_count", 0) or 0,
        total_tokens=getattr(usage, "total_token_count", 0) or 0,
        latency_ms=latency_ms,
        verdict=verdict,
    )


def _cost(model: str, in_tokens: float, out_tokens: float, thinking: float) -> float:
    if model not in PRICING:
        return float("nan")
    p_in, p_out = PRICING[model]
    # Thinking tokens bill at the output rate.
    return (in_tokens * p_in + (out_tokens + thinking) * p_out) / 1_000_000


def _fmt_money(n: float) -> str:
    if n != n:  # NaN
        return "?"
    if n < 0.001:
        return f"${n * 100:.4f}¢"  # use micro-cents for tiny numbers
    return f"${n:.6f}"


def run(models: list[str], runs_per_case: int) -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("error: GEMINI_API_KEY not set", file=sys.stderr)
        return 2

    from google.genai import types as types_mod  # type: ignore
    client = gemini._get_client()

    print(f"# grader cost — {len(CASES)} cases × {runs_per_case} run(s) per model\n")

    rows: list[dict] = []
    for model in models:
        samples: list[Sample] = []
        matches = 0
        checked = 0
        print(f"-- {model}")
        for case in CASES:
            for _ in range(runs_per_case):
                s = _grade_once(client, types_mod, model, case)
                samples.append(s)
                if s.error:
                    print(f"   ✗ {case.label}: {s.error}")
                    continue
                ok_mark = ""
                if case.expected:
                    checked += 1
                    if s.verdict == case.expected:
                        matches += 1
                        ok_mark = " ✓"
                    else:
                        ok_mark = f" ✗(expected {case.expected})"
                print(
                    f"   {case.label}: verdict={s.verdict}{ok_mark} "
                    f"in={s.prompt_tokens} out={s.output_tokens} "
                    f"think={s.thinking_tokens} ({s.latency_ms:.0f}ms)"
                )

        good = [s for s in samples if s.error is None]
        if not good:
            print(f"   (no successful calls for {model})")
            rows.append({"model": model, "ok": False})
            continue
        avg_in   = statistics.mean(s.prompt_tokens for s in good)
        avg_out  = statistics.mean(s.output_tokens for s in good)
        avg_think = statistics.mean(s.thinking_tokens for s in good)
        avg_total = statistics.mean(s.total_tokens for s in good)
        avg_lat   = statistics.mean(s.latency_ms for s in good)
        avg_cost  = _cost(model, avg_in, avg_out, avg_think)
        rows.append({
            "model": model, "ok": True,
            "in": avg_in, "out": avg_out, "think": avg_think,
            "total": avg_total, "latency_ms": avg_lat,
            "cost_per_call": avg_cost,
            "calls_per_dollar": (1.0 / avg_cost) if avg_cost > 0 else float("inf"),
            "match": f"{matches}/{checked}" if checked else "—",
        })
        print()

    # Markdown table — sorted ascending by per-call cost so the cheapest
    # working model lands at the top.
    print("\n## summary (averages per missed-answer call)\n")
    hdr = (
        "| model | match | in | out | think | latency | $ / call | calls / $1 |"
    )
    sep = "|" + "|".join(["---"] * (hdr.count("|") - 1)) + "|"
    print(hdr)
    print(sep)
    ok_rows = [r for r in rows if r.get("ok")]
    ok_rows.sort(key=lambda r: r["cost_per_call"])
    for r in ok_rows:
        cpd = r["calls_per_dollar"]
        cpd_s = f"{cpd:,.0f}" if cpd != float("inf") else "—"
        print(
            f"| {r['model']} | {r['match']} "
            f"| {r['in']:.0f} | {r['out']:.0f} | {r['think']:.0f} "
            f"| {r['latency_ms']:.0f}ms "
            f"| {_fmt_money(r['cost_per_call'])} | {cpd_s} |"
        )
    for r in rows:
        if not r.get("ok"):
            print(f"| {r['model']} | n/a — all calls failed | — | — | — | — | — | — |")

    print(
        "\nPricing snapshot 2026-05-10 from ai.google.dev/pricing — re-check "
        "before quoting absolute numbers. Thinking tokens bill at the output "
        "rate. Latency is wall-clock from this script's location."
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--models",
        default=",".join([
            "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite",
            "gemini-3-pro-preview",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
        ]),
        help="comma-separated model ids to compare",
    )
    ap.add_argument(
        "--list", action="store_true",
        help="list every model the API exposes and exit (no grading)",
    )
    ap.add_argument(
        "--runs", type=int, default=1,
        help="how many times to call each case per model (default 1)",
    )
    args = ap.parse_args()
    if args.list:
        # Dump every model the key can access — names normalized to the
        # short form generate_content accepts. Handy when Google rolls
        # a new preview and the cost script's default list is stale.
        client = gemini._get_client()
        for m in client.models.list():
            name = m.name or ""
            print(name.removeprefix("models/"))
        return 0
    return run([m.strip() for m in args.models.split(",") if m.strip()], args.runs)


if __name__ == "__main__":
    raise SystemExit(main())
