"""Unit tests for the generous typed-answer matcher."""

import pytest

from kana_quiz.grading import (
    accepted_meanings,
    grade_typed,
    normalize_english,
    normalize_kana,
)
from kana_quiz.models import Word


def make_word(*, kana: str = "りんご", english: str = "apple", kanji: str | None = "林檎") -> Word:
    return Word(
        id=1,
        kana=kana,
        english=english,
        kanji=kanji,
        tags=(),
        deck_id=1,
    )


def test_normalize_kana_strips_and_unifies_long_vowel() -> None:
    assert normalize_kana("  コーヒー  ") == "コーヒー"
    assert normalize_kana("コ-ヒ-") == "コーヒー"
    assert normalize_kana("コ‐ヒ‐") == "コーヒー"


def test_normalize_english_lowercases_strips_articles() -> None:
    assert normalize_english("  To Eat. ") == "eat"
    assert normalize_english("The Mountain") == "mountain"
    assert normalize_english("an apple") == "apple"


def test_accepted_meanings_splits_separators() -> None:
    assert accepted_meanings("to eat / to consume; devour, munch") == [
        "eat",
        "consume",
        "devour",
        "munch",
    ]


def test_accepted_meanings_strips_parentheticals() -> None:
    # JMdict-style clarifiers: typing the bare noun should grade correct.
    meanings = accepted_meanings("minister (cabinet)")
    assert "minister" in meanings


def test_ja2en_parenthetical_clarifier_accepted() -> None:
    word = make_word(english="visit (to a sick person)")
    assert grade_typed("visit", word, "ja2en") is True


def test_separators_inside_parenthetical_do_not_split() -> None:
    # Disambiguated glosses ("order (sequence, arrangement)") put a comma
    # inside the clarifier. Splitting there would strand "order (sequence"
    # with no closing paren to strip, so the bare core would never match.
    assert accepted_meanings("order (sequence, arrangement)") == [
        "order (sequence, arrangement)",
        "order",
    ]


def test_ja2en_bare_core_of_disambiguated_gloss_accepted() -> None:
    word = make_word(english="order (a command, an instruction)")
    assert grade_typed("order", word, "ja2en") is True


def test_ja2en_one_of_many_synonyms_accepted() -> None:
    word = make_word(english="somewhat, a little, more or less")
    assert grade_typed("a little", word, "ja2en") is True
    assert grade_typed("somewhat", word, "ja2en") is True


def test_en2ja_exact_kana_correct() -> None:
    word = make_word(kana="りんご")
    assert grade_typed("りんご", word, "en2ja") is True


def test_en2ja_kanji_also_accepted() -> None:
    word = make_word(kana="りんご", kanji="林檎")
    assert grade_typed("林檎", word, "en2ja") is True


def test_cloze_conjugated_kanji_surface_accepts_kana_reading() -> None:
    word = make_word(kana="あつかう", kanji="扱う", english="to handle")

    assert grade_typed(
        "あつかってください",
        word,
        "en2ja",
        cloze_expected="扱ってください",
    ) is True
    assert grade_typed(
        "あつかって",
        word,
        "en2ja",
        cloze_expected="扱ってください",
    ) is True


def test_cloze_na_adjective_accepts_kana_reading_without_na() -> None:
    word = make_word(kana="きみょう", kanji="奇妙", english="strange")

    assert grade_typed("きみょうな", word, "en2ja", cloze_expected="奇妙な") is True
    assert grade_typed("きみょう", word, "en2ja", cloze_expected="奇妙な") is True


def test_en2ja_typo_within_threshold_correct() -> None:
    word = make_word(kana="ありがとう")
    # one-char swap: ありがとお → still close enough
    assert grade_typed("ありがとお", word, "en2ja") is True


def test_en2ja_typo_beyond_threshold_incorrect() -> None:
    word = make_word(kana="りんご")
    # three-char word, totally different — must fail
    assert grade_typed("ねこ", word, "en2ja") is False


def test_en2ja_long_vowel_marks_normalized() -> None:
    word = make_word(kana="コーヒー")
    assert grade_typed("コ-ヒ-", word, "en2ja") is True


def test_ja2en_exact_match() -> None:
    word = make_word(english="apple")
    assert grade_typed("apple", word, "ja2en") is True


def test_ja2en_synonym_accepted() -> None:
    word = make_word(english="to eat / to consume")
    assert grade_typed("consume", word, "ja2en") is True
    assert grade_typed("eat", word, "ja2en") is True


def test_ja2en_article_stripped() -> None:
    word = make_word(english="to eat")
    assert grade_typed("eat", word, "ja2en") is True
    assert grade_typed("To Eat.", word, "ja2en") is True


def test_ja2en_minor_typo_accepted() -> None:
    word = make_word(english="mountain")
    assert grade_typed("mountian", word, "ja2en") is True


def test_ja2en_unrelated_word_incorrect() -> None:
    word = make_word(english="apple")
    assert grade_typed("banana", word, "ja2en") is False


@pytest.mark.parametrize("typed", ["", "   "])
def test_empty_input_never_correct(typed: str) -> None:
    word = make_word()
    assert grade_typed(typed, word, "en2ja") is False
    assert grade_typed(typed, word, "ja2en") is False
