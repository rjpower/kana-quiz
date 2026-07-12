"""Unit tests for the cloze span locator, esp. its kanji_variants path.

The locator is the single point of truth for "where in this cached
Gemini sentence does the target word appear?". The kanji-stem heuristic
covers the common case, but two failure modes need the kanji_variants
table to recover:

1. The sentence uses a homograph-by-reading kanji variant (食料 vs 食糧,
   both しょくりょう). The card's stored kanji form misses, but the
   reading is the bridge.
2. The word has no kanji form on file (e.g. できるだけ) yet the
   sentence writes the kanji form anyway (出来るだけ).

We seed an in-memory kanji_variants table with the two cases above and
assert the locator falls through cleanly to the variants path.
"""

from __future__ import annotations

import sqlite3

import pytest

from kana_quiz.gemini import locate_target_form
from kana_quiz.models import Word


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE kanji_variants (
          kana TEXT NOT NULL,
          kanji TEXT NOT NULL,
          PRIMARY KEY (kana, kanji)
        )
        """
    )
    conn.execute(
        "CREATE INDEX ix_kanji_variants_kana ON kanji_variants(kana)"
    )
    conn.executemany(
        "INSERT INTO kanji_variants (kana, kanji) VALUES (?, ?)",
        [
            ("しょくりょう", "食料"),
            ("しょくりょう", "食糧"),
            ("できるだけ", "出来るだけ"),
            ("できるだけ", "出来る丈"),
        ],
    )
    return conn


def _word(**kw) -> Word:
    defaults = dict(
        id=1,
        kana="",
        english="",
        kanji=None,
        tags=(),
        deck_id=0,
    )
    defaults.update(kw)
    return Word(**defaults)


def test_alternate_kanji_with_shared_reading_resolves_via_variants():
    conn = _make_conn()
    word = _word(kana="しょくりょう", kanji="食料", english="food, foodstuff")
    sentence = "災害に備えて食糧を準備しました。"
    assert locate_target_form(sentence, word, conn) == "食糧"


def test_kana_only_word_with_kanji_in_sentence_resolves_via_variants():
    conn = _make_conn()
    word = _word(kana="できるだけ", kanji=None, english="as much as possible")
    sentence = "出来るだけ野菜を食べてください。"
    assert locate_target_form(sentence, word, conn) == "出来るだけ"


def test_variants_path_not_used_when_primary_kanji_stem_matches():
    """If the deck's own kanji form is in the sentence, that wins."""
    conn = _make_conn()
    word = _word(kana="しょくりょう", kanji="食料", english="food, foodstuff")
    sentence = "毎日の食料を買いに行く。"
    assert locate_target_form(sentence, word, conn) == "食料"


def test_no_conn_still_works_for_simple_cases():
    """Backward-compat: callers without a connection get the old behavior."""
    word = _word(kana="やくだつ", kanji="役立つ", english="to be useful")
    sentence = "この辞書は勉強にとても役立ちます。"
    assert locate_target_form(sentence, word, None) == "役立ちます"


def test_no_match_returns_none():
    conn = _make_conn()
    word = _word(kana="しょくりょう", kanji="食料", english="food")
    sentence = "今日は天気がいいですね。"  # neither word nor variant present
    assert locate_target_form(sentence, word, conn) is None


def test_empty_sentence_returns_none():
    conn = _make_conn()
    word = _word(kana="しょくりょう", kanji="食料", english="food")
    assert locate_target_form("", word, conn) is None


def test_variants_path_refuses_when_kana_only_word_has_ambiguous_match():
    """Single-mora readings like か map to dozens of unrelated kanji
    (課, 日, 蚊, 鹿, …). When the deck word has no kanji to filter
    against and multiple distinct variants appear in the sentence, the
    locator must refuse rather than guess.

    Regression: deck entry kanji='～か～', kana='か' (the counter-suffix
    か in 第〜課) cached an empty target_form. The variant lookup
    accepted whichever single-char kanji it iterated first, putting the
    cloze blank at 日 (in 今日) instead of 課.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE kanji_variants (
          kana TEXT NOT NULL,
          kanji TEXT NOT NULL,
          PRIMARY KEY (kana, kanji)
        )
        """
    )
    conn.executemany(
        "INSERT INTO kanji_variants (kana, kanji) VALUES (?, ?)",
        [("か", "課"), ("か", "日"), ("か", "蚊"), ("か", "鹿")],
    )
    # The tilde-decorated kanji has no actual kanji chars, so the
    # locator can't determine own_lead_kanji and falls into the
    # variant-lookup path with no homophone filter.
    word = _word(kana="か", kanji="～か～", english="lesson, chapter")
    sentence = "今日は教科書の第五課を勉強します。"
    assert locate_target_form(sentence, word, conn) is None


def test_variants_path_rejects_homophone_different_word():
    """Different lemmata sharing a reading must NOT be treated as variants.

    Deck has 商人 (しょうにん, "merchant"). Sentence is about 承認
    (also しょうにん, "approval"). The variants table — built from
    JMdict — groups every entry sharing しょうにん, including 承認.
    The locator's leading-kanji filter must reject 承認 because the
    deck word's first kanji is 商, not 承 — otherwise Gemini's
    wrong-word picks become invisible to the audit step.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE kanji_variants (
          kana TEXT NOT NULL,
          kanji TEXT NOT NULL,
          PRIMARY KEY (kana, kanji)
        )
        """
    )
    conn.executemany(
        "INSERT INTO kanji_variants (kana, kanji) VALUES (?, ?)",
        [
            ("しょうにん", "商人"),
            ("しょうにん", "承認"),
            ("しょうにん", "証人"),
        ],
    )
    word = _word(kana="しょうにん", kanji="商人", english="merchant, trader")
    sentence = "社長が新しいプロジェクトを承認しました。"
    # 承認 lives in the variants table but its leading kanji 承 ≠ 商,
    # so the homophone filter must reject it and the locator returns
    # None (no match) — signaling the audit that this sentence
    # genuinely needs a regen.
    assert locate_target_form(sentence, word, conn) is None
