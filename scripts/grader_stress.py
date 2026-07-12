"""Stress-test two Gemini grader models against each other on N cases.

Generates a deterministic batch of (target word, student-typed answer)
cases from the live deck, runs them through both models in parallel,
then prints:

  * verdict-distribution per model (correct / accept / incorrect),
  * pairwise agreement %,
  * every disagreement verbatim so the user can judge which call
    was right.

Case-generation strategies, mixed to cover the verdict spectrum:

  - perfect: typed = the reference English / kana exactly        → expect "correct"
  - typo:    typed = a 1-char-edit of the reference              → expect "correct"
  - particle-drop: en2ja, typed = strip leading "to ", or for
                   kana drop a trailing particle (に, を, etc)    → expect ~"correct"
  - kanji-sibling: typed = a different deck word sharing ≥1 kanji
                   with the target (confusable pair)             → expect "accept" or "incorrect"
  - reading-sibling: typed = another deck word with the same
                     first kana (mild confusable)                → mostly "incorrect"
  - random-wrong: typed = an unrelated deck word's answer        → expect "incorrect"
  - empty:   typed = ""                                          → expect "incorrect"

Run:
    GEMINI_API_KEY=... uv run python scripts/grader_stress.py
    GEMINI_API_KEY=... uv run python scripts/grader_stress.py --n 200 \
        --models gemini-3-flash-preview,gemini-3.1-flash-lite
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from kana_quiz import gemini  # noqa: E402
from kana_quiz.models import Word  # noqa: E402


@dataclass(frozen=True)
class Case:
    label: str            # generation strategy
    word: Word
    typed: str
    direction: str        # "ja2en" or "en2ja"


def _load_words(db_path: Path) -> list[Word]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, kana, english, kanji
          FROM words
         WHERE ignored_at IS NULL
           AND english IS NOT NULL AND english != ''
           AND kana IS NOT NULL AND kana != ''
        """
    ).fetchall()
    conn.close()
    return [
        Word(id=r["id"], kana=r["kana"], english=r["english"],
             kanji=r["kanji"], tags=(), deck_id=0)
        for r in rows
    ]


def _strip_articles(en: str) -> str:
    """Mirror the deck's normalization — strip a leading 'to '/'a '/'the '."""
    s = en.strip().lower()
    for p in ("to ", "the ", "an ", "a "):
        if s.startswith(p):
            return s[len(p):]
    return s


def _first_gloss(en: str) -> str:
    """Return the first comma-separated gloss token, stripped."""
    return (en or "").split(",")[0].strip()


def _typo(s: str) -> str:
    """Drop one character to simulate a typing slip."""
    if len(s) < 4:
        return s
    i = len(s) // 2
    return s[:i] + s[i + 1:]


def _drop_trailing_particle(kana: str) -> str:
    """For kana ending in a trailing particle (に / を / で / の / と),
    return the stem; otherwise just drop the last char."""
    if len(kana) <= 1:
        return kana
    if kana[-1] in "にをでのとはがも":
        return kana[:-1]
    return kana


def _share_kanji(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    sa = set(a)
    sb = set(b)
    return any(ch in sb for ch in sa if "一" <= ch <= "鿿")


def _build_cases(words: list[Word], n: int, seed: int) -> list[Case]:
    rng = random.Random(seed)
    by_kanji_char: dict[str, list[Word]] = defaultdict(list)
    by_first_kana: dict[str, list[Word]] = defaultdict(list)
    for w in words:
        if w.kanji:
            for ch in w.kanji:
                if "一" <= ch <= "鿿":
                    by_kanji_char[ch].append(w)
        if w.kana:
            by_first_kana[w.kana[0]].append(w)

    # Generation strategy mix (over n cases).
    strategies = (
        ["perfect"]         * (n * 15 // 100) +
        ["typo"]            * (n * 15 // 100) +
        ["particle-drop"]   * (n * 10 // 100) +
        ["kanji-sibling"]   * (n * 25 // 100) +
        ["reading-sibling"] * (n * 10 // 100) +
        ["random-wrong"]    * (n * 20 // 100) +
        ["empty"]           * (n *  5 // 100)
    )
    while len(strategies) < n:
        strategies.append("random-wrong")
    rng.shuffle(strategies)

    cases: list[Case] = []
    for strategy in strategies:
        # Sample a target. Some strategies need extra constraints; retry
        # until we hit one that works (bounded so we never spin forever).
        for _ in range(20):
            target = rng.choice(words)
            direction = rng.choice(("ja2en", "en2ja"))
            typed: str | None = None
            label = strategy

            if strategy == "perfect":
                if direction == "ja2en":
                    typed = _first_gloss(target.english)
                else:
                    typed = target.kana

            elif strategy == "typo":
                if direction == "ja2en":
                    base = _first_gloss(target.english)
                else:
                    base = target.kana
                if len(base) < 4:
                    continue
                typed = _typo(base)

            elif strategy == "particle-drop":
                if direction == "ja2en":
                    raw = _first_gloss(target.english)
                    stripped = _strip_articles(raw)
                    if stripped == raw.lower():
                        continue
                    typed = stripped
                else:
                    new = _drop_trailing_particle(target.kana)
                    if new == target.kana:
                        continue
                    typed = new

            elif strategy == "kanji-sibling":
                if not target.kanji:
                    continue
                # Pull a deck word that shares ≥1 kanji but is a different word.
                candidates: list[Word] = []
                for ch in target.kanji:
                    if "一" <= ch <= "鿿":
                        for w in by_kanji_char.get(ch, ()):
                            if w.id != target.id and w.kana != target.kana:
                                candidates.append(w)
                if not candidates:
                    continue
                sibling = rng.choice(candidates)
                if direction == "ja2en":
                    typed = _first_gloss(sibling.english)
                else:
                    typed = sibling.kana

            elif strategy == "reading-sibling":
                first = target.kana[0]
                pool = [w for w in by_first_kana.get(first, ())
                        if w.id != target.id]
                if not pool:
                    continue
                sibling = rng.choice(pool)
                if direction == "ja2en":
                    typed = _first_gloss(sibling.english)
                else:
                    typed = sibling.kana

            elif strategy == "random-wrong":
                other = rng.choice(words)
                if other.id == target.id:
                    continue
                if direction == "ja2en":
                    typed = _first_gloss(other.english)
                else:
                    typed = other.kana

            elif strategy == "empty":
                typed = ""

            if typed is None:
                continue
            cases.append(Case(label=label, word=target, typed=typed, direction=direction))
            break
    return cases


@dataclass
class Result:
    model: str
    case_idx: int
    verdict: str | None
    explanation: str
    alternates: tuple[str, ...]
    clarified_gloss: str | None
    latency_ms: float
    error: str | None = None


def _grade(model: str, case_idx: int, case: Case) -> Result:
    t0 = time.perf_counter()
    try:
        grade = gemini.grade_semantic(
            case.word, case.typed, case.direction, model=model
        )
    except gemini.GeminiUnavailable as e:
        return Result(
            model=model, case_idx=case_idx, verdict=None,
            explanation="", alternates=(), clarified_gloss=None,
            latency_ms=(time.perf_counter() - t0) * 1000,
            error=str(e),
        )
    return Result(
        model=model, case_idx=case_idx, verdict=grade.verdict,
        explanation=grade.explanation, alternates=grade.alternates,
        clarified_gloss=grade.clarified_gloss,
        latency_ms=(time.perf_counter() - t0) * 1000,
    )


def run(args: argparse.Namespace) -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("error: GEMINI_API_KEY not set", file=sys.stderr)
        return 2
    words = _load_words(args.db)
    if not words:
        print(f"error: no usable words in {args.db}", file=sys.stderr)
        return 2

    cases = _build_cases(words, args.n, seed=args.seed)
    print(f"# grader stress — {len(cases)} cases, seed={args.seed}")
    print(f"# strategy mix: { dict(Counter(c.label for c in cases)) }")
    print(f"# models: {args.models}\n")

    # 100 cases × 2 models = 200 calls. With concurrency=16 this lands
    # well under a minute and stays comfortably below typical RPM caps.
    work: list[tuple[str, int, Case]] = [
        (model, i, case)
        for model in args.models
        for i, case in enumerate(cases)
    ]
    results_by_model: dict[str, list[Result | None]] = {
        m: [None] * len(cases) for m in args.models
    }
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {
            ex.submit(_grade, model, i, case): (model, i)
            for model, i, case in work
        }
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            model, i = futures[fut]
            r = fut.result()
            results_by_model[model][i] = r
            done_count += 1
            if done_count % 25 == 0:
                elapsed = time.perf_counter() - t0
                rate = done_count / elapsed if elapsed > 0 else 0
                print(f"  ... {done_count}/{len(work)} ({rate:.1f}/s)")
    total_secs = time.perf_counter() - t0
    print(f"\ndone in {total_secs:.1f}s\n")

    # Per-model verdict distribution.
    print("## verdict distribution\n")
    print("| model | correct | accept | incorrect | error | avg latency |")
    print("|---|---|---|---|---|---|")
    for model in args.models:
        rs = results_by_model[model]
        c = Counter((r.verdict if r and not r.error else ("error" if r else "missing")) for r in rs)
        good = [r for r in rs if r and not r.error]
        avg_lat = sum(r.latency_ms for r in good) / len(good) if good else 0
        print(
            f"| {model} | {c.get('correct', 0)} | {c.get('accept', 0)} "
            f"| {c.get('incorrect', 0)} | {c.get('error', 0) + c.get('missing', 0)} "
            f"| {avg_lat:.0f}ms |"
        )

    # Pairwise agreement (only when we have ≥2 models).
    if len(args.models) >= 2:
        baseline = args.models[0]
        print(f"\n## agreement with `{baseline}`\n")
        print("| model | exact-match | pass/fail-match (corr+acc → pass) |")
        print("|---|---|---|")
        base_rs = results_by_model[baseline]
        for model in args.models[1:]:
            other_rs = results_by_model[model]
            exact = 0
            passfail = 0
            checked = 0
            for a, b in zip(base_rs, other_rs):
                if not a or not b or a.error or b.error:
                    continue
                checked += 1
                if a.verdict == b.verdict:
                    exact += 1
                if _passes(a.verdict) == _passes(b.verdict):
                    passfail += 1
            print(
                f"| {model} | {exact}/{checked} ({100 * exact / checked:.0f}%) "
                f"| {passfail}/{checked} ({100 * passfail / checked:.0f}%) |"
            )

        # Spell out every disagreement so the user can judge which call
        # was right. Limit to a configurable cap so the output stays
        # readable on 200-case runs.
        print(f"\n## disagreements (showing up to {args.show_disagreements})\n")
        shown = 0
        for i, case in enumerate(cases):
            ra = results_by_model[baseline][i]
            rb = results_by_model[args.models[1]][i]
            if not ra or not rb or ra.error or rb.error:
                continue
            if ra.verdict == rb.verdict:
                continue
            shown += 1
            if shown > args.show_disagreements:
                break
            tgt = (
                f"{case.word.kana}"
                + (f" ({case.word.kanji})" if case.word.kanji else "")
                + f" — {case.word.english}"
            )
            print(f"#{i} [{case.label} {case.direction}] target: {tgt}")
            print(f"     typed: {case.typed!r}")
            print(f"     {baseline}: {ra.verdict} — {ra.explanation}")
            print(f"     {args.models[1]}: {rb.verdict} — {rb.explanation}")
            print()
    return 0


def _passes(v: str | None) -> bool:
    return v in ("correct", "accept")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=100, help="number of cases")
    ap.add_argument("--seed", type=int, default=20260510,
                    help="rng seed — reuse the same value to compare runs")
    ap.add_argument(
        "--models",
        default="gemini-3-flash-preview,gemini-3.1-flash-lite",
        help="comma-separated model ids (first one is the baseline)",
    )
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument(
        "--show-disagreements", type=int, default=50,
        help="max number of disagreement rows to print",
    )
    ap.add_argument(
        "--db", type=Path,
        default=ROOT / "data" / "kana_quiz.sqlite",
        help="sqlite db to draw words from",
    )
    args = ap.parse_args()
    args.models = [m.strip() for m in args.models.split(",") if m.strip()]
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
