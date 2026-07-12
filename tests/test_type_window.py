"""Estimator tests for the type-in countdown window.

The timer is sized by how many keystrokes the user is expected to
press, NOT by glyph count. These tests pin down the romaji estimator
and the gloss-averaging so a refactor can't silently shift either.
"""

from __future__ import annotations

from kana_quiz.models import Word
from kana_quiz.routes.session import (
    TYPE_BASE_MS,
    TYPE_MAX_MS,
    TYPE_MIN_MS,
    TYPE_PER_CHAR_MS,
    _avg_gloss_len,
    _expected_keystrokes,
    _romaji_keystrokes,
    _type_window_ms,
)


def W(kana: str = "", english: str = "", kanji: str | None = None) -> Word:
    return Word(id=0, kana=kana, english=english, kanji=kanji, tags=(), deck_id=0)


def test_romaji_keystrokes_basic_gojuon() -> None:
    # Vowels and consonant rows.
    assert _romaji_keystrokes("ねこ") == 4         # ne(2) + ko(2)
    assert _romaji_keystrokes("ありがとう") == 8   # a(1)+ri(2)+ga(2)+to(2)+u(1)
    # The three "extra letter" kana.
    assert _romaji_keystrokes("し") == 3          # shi
    assert _romaji_keystrokes("ち") == 3          # chi
    assert _romaji_keystrokes("つ") == 3          # tsu


def test_romaji_keystrokes_small_kana_combos() -> None:
    # きゃ → "kya" (3): き normally costs 2 ("ki"), the small ゃ drops
    # the trailing "i" and adds "ya" — net +1, total 3.
    assert _romaji_keystrokes("きゃ") == 3
    # じゅ → "ju": じ is 2 ("ji"), ゅ drops "i" and adds "yu" → 1 + 2 = 3.
    # Wait — じ = "ji" (2), -i (drop trailing) = "j" (1), +yu = "jyu" (3)
    # Hepburn collapses this to "ju" (2) in production IMEs, but our
    # estimator stays close to literal letter-replacement and lands at 3.
    # That's fine: the timer is generous either way.
    assert _romaji_keystrokes("じゅ") == 3
    # しゃ: sh(2 from shi - i) + ya(2) = 4. Real Hepburn writes "sha"
    # which is 3. Our estimator's slight over-count just buys the user
    # a hair more time — acceptable.
    assert _romaji_keystrokes("しゃ") == 4


def test_romaji_keystrokes_sokuon_and_special() -> None:
    # かった → "katta" = 5 keystrokes (ka + sokuon-doubled t + ta).
    # ka(2) + っ(1, doubles next consonant) + ta(2) = 5.
    assert _romaji_keystrokes("かった") == 5
    # ん is one keystroke.
    assert _romaji_keystrokes("ほん") == 3       # ho(2) + n(1)
    # Long-vowel mark ー counts as one: コ(2) + ー(1) + ヒ(2) + ー(1).
    assert _romaji_keystrokes("コーヒー") == 6


def test_romaji_keystrokes_real_words() -> None:
    # Sanity-check a few realistic deck entries.
    assert _romaji_keystrokes("おはよう") == 6                 # o+ha+yo+u = 1+2+2+1
    assert _romaji_keystrokes("おはようございます") == 15     # +go+za+i+ma+su = 6+2+2+1+2+2
    # じゅうよう: じゅ(3) + う(1) + よ(2) + う(1) = 7. Production
    # Hepburn writes "juuyou" (6) — our estimator over-counts by 1
    # here, which the timer can absorb without complaint.
    assert _romaji_keystrokes("じゅうよう") == 7


def test_avg_gloss_len() -> None:
    # Single gloss: just its length.
    assert _avg_gloss_len("cat") == 3
    # Two equal-length glosses average to that length.
    assert _avg_gloss_len("important, essential") == 9
    # Mixed lengths: integer mean rounded down.
    # "law"=3, "rule"=4, "principle"=9 → mean=16/3=5
    assert _avg_gloss_len("law, rule, principle") == 5
    # Empties and whitespace.
    assert _avg_gloss_len("") == 0
    assert _avg_gloss_len("   ") == 0
    # Whitespace-padded entries are stripped before measuring.
    assert _avg_gloss_len("a , b , c") == 1


def test_expected_keystrokes_routes_to_correct_estimator() -> None:
    w = W(kana="じゅうよう", english="important, essential")
    # ja2en uses the gloss-average — both glosses are 9 chars → 9.
    assert _expected_keystrokes("ja2en", w) == 9
    # en2ja uses the romaji estimator on kana.
    assert _expected_keystrokes("en2ja", w) == _romaji_keystrokes("じゅうよう")


def test_type_window_floor_and_ceiling() -> None:
    # Below the floor: short kana clamps to the 8s minimum.
    assert _type_window_ms(1) == TYPE_MIN_MS  # 5000 + 600 = 5600 < 8000
    assert _type_window_ms(5) == TYPE_BASE_MS + TYPE_PER_CHAR_MS * 5  # 8000 exactly
    # Above the ceiling: super-long answers clamp to the 25s max.
    huge = TYPE_MAX_MS // TYPE_PER_CHAR_MS + 100
    assert _type_window_ms(huge) == TYPE_MAX_MS
    # Mid-range scales linearly.
    assert _type_window_ms(10) == TYPE_BASE_MS + TYPE_PER_CHAR_MS * 10


def test_full_timer_for_real_cards() -> None:
    """End-to-end: feed a Word + direction through the full estimator
    chain and confirm the produced window matches what the comment
    in routes/session.py advertises."""
    # 重要 / "important, essential" — gloss average is 9, so ja2en
    # used to claim 17s with the old "whole-string length" formula
    # (20 chars). New formula: 5000 + 600*9 = 10400.
    juuyou = W(kana="じゅうよう", english="important, essential")
    assert _type_window_ms(_expected_keystrokes("ja2en", juuyou)) == 5000 + 600 * 9
    # おはようございます en2ja was 9 kana × 600 + 5000 = 10400 with the
    # glyph-count formula. Romaji is ~15 keystrokes → 5000 + 600*15 = 14000.
    assert _type_window_ms(_expected_keystrokes("en2ja", W(kana="おはようございます"))) > 13000
