"""Direction balancing + recognition-first new-word debut in pick_next_card.

The 12h pair-snooze deliberately buries a card's sibling direction (good SRS),
but that lets one direction's cohort dominate a day's due queue. pick_next_card
takes a `prefer_direction` hint so callers can interleave recognition/production
within a session, and new words now debut ja→en (recognition before production).
"""

import sqlite3

import pytest

from kana_quiz.session import pick_next_card

PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


@pytest.fixture
def deck(tmp_path, monkeypatch):
    from kana_quiz.db import connect, init_schema

    path = tmp_path / "db.sqlite"
    monkeypatch.setenv("KANA_QUIZ_DB", str(path))
    init_schema()
    conn = connect()
    deck_id = conn.execute("SELECT id FROM decks WHERE name = 'Default'").fetchone()["id"]
    for kana, english in [("いぬ", "dog"), ("ねこ", "cat"), ("うま", "horse"), ("とり", "bird")]:
        conn.execute(
            "INSERT INTO words (kana, english, deck_id) VALUES (?, ?, ?)",
            (kana, english, deck_id),
        )
    yield conn
    conn.close()


def _add_state(conn: sqlite3.Connection, word_id: int, task: str, due: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO task_state "
        "(word_id, task, ease, interval_days, repetitions, due_at, introduced_at) "
        "VALUES (?, ?, 2.5, 5.0, 3, ?, ?)",
        (word_id, task, due, PAST),
    )


def _word_ids(conn: sqlite3.Connection) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM words ORDER BY id").fetchall()]


def test_recognition_first_new_word_debuts_ja2en(deck):
    # A fresh word (no task_state) is introduced in the recognition direction so
    # the learner meets meaning before being asked to produce.
    pick = pick_next_card(deck)
    assert pick is not None
    assert pick.just_introduced is True
    assert pick.direction == "ja2en"


def test_prefer_direction_serves_that_direction(deck):
    for wid in _word_ids(deck):
        _add_state(deck, wid, "en2ja", PAST)
        _add_state(deck, wid, "ja2en", PAST)
    # Both directions are due for every word; the hint alone decides which we get.
    assert pick_next_card(deck, prefer_direction="en2ja").direction == "en2ja"
    assert pick_next_card(deck, prefer_direction="ja2en").direction == "ja2en"


def test_prefer_direction_falls_back_when_none_due(deck):
    # Only ja2en is due; asking for en2ja must not strand the round — it serves
    # the ja2en that is actually due rather than returning nothing.
    for wid in _word_ids(deck):
        _add_state(deck, wid, "ja2en", PAST)
        _add_state(deck, wid, "en2ja", FUTURE)
    pick = pick_next_card(deck, prefer_direction="en2ja")
    assert pick is not None
    assert pick.direction == "ja2en"


def test_no_hint_keeps_global_soonest(deck):
    # Without a hint, behaviour is unchanged: the globally-soonest due card wins,
    # regardless of direction. Every word is due (so the picker serves a review
    # rather than introducing a new word) and one en2ja card is clearly soonest.
    ids = _word_ids(deck)
    for wid in ids:
        _add_state(deck, wid, "ja2en", "2022-06-01T00:00:00+00:00")
    _add_state(deck, ids[0], "en2ja", "2021-06-01T00:00:00+00:00")  # globally soonest
    pick = pick_next_card(deck)
    assert pick is not None
    assert pick.word.id == ids[0]
    assert pick.direction == "en2ja"
