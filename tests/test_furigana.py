"""Furigana segmentation: okurigana alignment + plain-run handling."""

from kana_quiz.furigana import furigana_segments


def _pairs(text: str) -> list[tuple[str, str | None]]:
    return [(s.t, s.r) for s in furigana_segments(text)]


def test_kanji_runs_get_readings_kana_stays_plain() -> None:
    # 私=わたし / 毎日=まいにち / 勉強=べんきょう; particles + する stay plain.
    assert _pairs("私は毎日勉強する") == [
        ("私", "わたし"),
        ("は", None),
        ("毎日", "まいにち"),
        ("勉強", "べんきょう"),
        ("する", None),
    ]


def test_okurigana_reading_sits_over_only_the_kanji() -> None:
    # 食べる: reading floats over 食 (た), the べる okurigana stays plain — the
    # tail isn't double-annotated with kana it already shows.
    segs = _pairs("食べる")
    assert ("食", "た") in segs
    assert ("べる", None) in segs
    # No segment carries a reading over kana-only text.
    assert all(r is None for t, r in segs if not any("一" <= c <= "鿿" for c in t))


def test_katakana_and_ascii_never_get_a_redundant_reading() -> None:
    # No kanji -> no ruby. Katakana keeps its own kana; digits pass through.
    assert _pairs("コーヒー") == [("コーヒー", None)]
    assert all(r is None for _, r in _pairs("2024"))


def test_pure_kana_sentence_is_a_single_plain_run() -> None:
    assert _pairs("ねこがすき") == [("ねこがすき", None)]


def test_empty_text_yields_no_segments() -> None:
    assert furigana_segments("") == []
