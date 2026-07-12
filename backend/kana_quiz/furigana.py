"""Furigana (reading) annotation for Japanese sentence text.

The quiz shows generated example sentences in several places — the two cloze
card frames, the post-answer example reveal, and the listening reveal. Beginners
can read kana but stall on kanji, so we annotate each kanji run with its reading
and ship the result as a list of ``RubySegment`` the frontend renders as HTML
``<ruby>``.

Readings come from :mod:`pykakasi`, a deterministic dictionary tokenizer — no
network, no LLM cost, and it works on every sentence already cached in sqlite
(unlike asking the generator to emit furigana, which would only cover newly
rolled rows). pykakasi disambiguates by longest-match rather than full
morphology, so a rare word or a rendaku reading is occasionally off; that's an
accepted trade for determinism and retroactive coverage.

The one bit of real work here is *okurigana alignment*: pykakasi hands back a
chunk like ``食べ`` / ``たべ`` (kanji head + trailing kana). Rendering ``たべ``
over the whole chunk would float the kana suffix's reading above kana it already
shows. :func:`_split_kanji_chunk` peels matching kana off both ends so the ruby
sits over just the kanji core (``食`` → ``た``, with ``べ`` left as plain text).
"""

from __future__ import annotations

from functools import lru_cache

from kana_quiz.schemas import RubySegment


def _is_kanji(ch: str) -> bool:
    """True for a CJK unified ideograph (the characters that take furigana)."""
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿"


@lru_cache(maxsize=1)
def _converter() -> object:
    """Process-wide pykakasi converter.

    Constructing it builds the trie from the bundled dictionary, so we do it
    once and reuse — the instance is stateless across ``convert`` calls.
    """
    from pykakasi import kakasi

    return kakasi()


def _split_kanji_chunk(orig: str, hira: str) -> list[RubySegment]:
    """Split a kanji-bearing chunk so the reading sits over only the kanji.

    ``orig`` contains at least one kanji and ``hira`` is its full reading.
    Matching kana are peeled off both ends (okurigana like the ``べ`` in
    ``食べ`` / ``たべ``, or a leading particle pykakasi merged in) and emitted as
    plain segments; the kanji core in the middle keeps the remaining reading.
    """
    prefix = ""
    core, reading = orig, hira
    # Peel a shared kana prefix (e.g. pykakasi merged a leading "お" or particle).
    while core and reading and not _is_kanji(core[0]) and core[0] == reading[0]:
        prefix += core[0]
        core, reading = core[1:], reading[1:]
    suffix = ""
    # Peel the shared okurigana tail.
    while core and reading and not _is_kanji(core[-1]) and core[-1] == reading[-1]:
        suffix = core[-1] + suffix
        core, reading = core[:-1], reading[:-1]

    segs: list[RubySegment] = []
    if prefix:
        segs.append(RubySegment(t=prefix))
    if core:
        # ``reading`` is empty only if the tails consumed everything, which
        # can't happen while a kanji remains in ``core``; guard anyway so we
        # never emit an empty <rt>.
        segs.append(RubySegment(t=core, r=reading or None))
    if suffix:
        segs.append(RubySegment(t=suffix))
    return segs


def _merge_plain(segs: list[RubySegment]) -> list[RubySegment]:
    """Coalesce adjacent reading-less segments into single text runs."""
    merged: list[RubySegment] = []
    for seg in segs:
        if seg.r is None and merged and merged[-1].r is None:
            merged[-1] = RubySegment(t=merged[-1].t + seg.t)
        else:
            merged.append(seg)
    return merged


def furigana_segments(text: str) -> list[RubySegment]:
    """Annotate ``text`` with kanji readings.

    Returns a list of ``RubySegment``: plain runs carry only ``t``; kanji runs
    also carry ``r`` (the hiragana reading) for the frontend to render as
    ``<ruby>t<rt>r</rt></ruby>``. Kana-only and ASCII chunks pass through as
    plain text — katakana words (no kanji) never get a redundant kana reading.
    """
    if not text:
        return []
    segs: list[RubySegment] = []
    for item in _converter().convert(text):  # type: ignore[attr-defined]
        orig = item["orig"]
        hira = item["hira"]
        if hira and hira != orig and any(_is_kanji(c) for c in orig):
            segs.extend(_split_kanji_chunk(orig, hira))
        else:
            segs.append(RubySegment(t=orig))
    return _merge_plain(segs)
