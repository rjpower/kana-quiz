"""Internal dataclasses used between the DB layer and the route handlers."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    id: int
    kana: str
    english: str
    kanji: str | None
    tags: tuple[str, ...]
    deck_id: int


def is_katakana_only(kana: str) -> bool:
    """Whether ``kana`` consists entirely of katakana characters.

    Loanwords/proper-noun loans (コンピュータ, ローマ) are written in pure
    katakana and the ja→en direction adds zero pedagogical value — the
    user reads the katakana phonetically and the answer is just the
    foreign word it transliterates. We use this to skip the ja2en card
    on creation (and to retroactively delete any existing rows in
    migration 005).

    Returns False on the empty string and on any string that contains a
    hiragana / kanji / latin character. Whitespace is ignored. The
    katakana Unicode block (U+30A0–U+30FF) already includes the chōonpu
    (ー, U+30FC) and small-kana variants, so a single range check covers
    everything we want to call "katakana-only".
    """
    if not kana:
        return False
    saw_kana = False
    for c in kana:
        if c.isspace():
            continue
        if not ("゠" <= c <= "ヿ"):
            return False
        saw_kana = True
    return saw_kana


def word_from_row(row: sqlite3.Row) -> Word:
    """Hydrate a :class:`Word` from a ``words`` row."""
    tags_raw = row["tags"] or ""
    tags = tuple(t for t in (x.strip() for x in tags_raw.split(",")) if t)
    return Word(
        id=row["id"],
        kana=row["kana"],
        english=row["english"],
        kanji=row["kanji"],
        tags=tags,
        deck_id=row["deck_id"],
    )
