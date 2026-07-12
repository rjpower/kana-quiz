"""Cached-alternates lookup path on the typed-answer grader.

These exercise the third match path added alongside the Gemini semantic
grader: once Gemini blesses a typed answer, the normalized variant is
persisted to ``word_alternates``; the next time the user types it, the
deterministic grader should accept it without another LLM round-trip.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from kana_quiz.db import connect, init_schema
from kana_quiz.grading import grade_typed
from kana_quiz.models import Word


def _make_word() -> Word:
    return Word(
        id=1,
        kana="りんご",
        english="apple",
        kanji="林檎",
        tags=(),
        deck_id=1,
    )


def _seed_word(conn) -> Word:
    """Insert a single word row so foreign keys against word_alternates resolve."""
    word = _make_word()
    conn.execute(
        "INSERT OR IGNORE INTO decks (id, name, level) VALUES (1, 'Default', 5)"
    )
    conn.execute(
        """
        INSERT INTO words (id, kana, english, kanji, tags, deck_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (word.id, word.kana, word.english, word.kanji, "", word.deck_id),
    )
    return word


@pytest.fixture
def db_with_word(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KANA_QUIZ_DB", str(tmp_path / "kq.sqlite"))
    init_schema()
    conn = connect()
    word = _seed_word(conn)
    try:
        yield conn, word
    finally:
        conn.close()


def test_alternate_match_with_conn(db_with_word) -> None:
    """A normalized alternate row should make a typed answer grade correct."""
    conn, word = db_with_word
    # "fruit" is unrelated to the reference meaning "apple" — the fuzzy
    # matcher rejects it, so a True result here proves the alternates
    # path fired.
    conn.execute(
        """
        INSERT INTO word_alternates (word_id, direction, alternate, source)
        VALUES (?, 'ja2en', 'fruit', 'gemini')
        """,
        (word.id,),
    )
    assert grade_typed("fruit", word, "ja2en", conn=conn) is True


def test_alternate_lookup_skipped_without_conn(db_with_word) -> None:
    """Without a connection the alternates path is unreachable, so fuzzy alone."""
    conn, word = db_with_word
    conn.execute(
        """
        INSERT INTO word_alternates (word_id, direction, alternate, source)
        VALUES (?, 'ja2en', 'fruit', 'gemini')
        """,
        (word.id,),
    )
    # Same input that *would* match via alternates — but conn=None disables
    # the lookup, so we fall back to fuzzy-only and "fruit" != "apple".
    assert grade_typed("fruit", word, "ja2en", conn=None) is False


def test_insert_or_ignore_dedupes_alternates(db_with_word) -> None:
    """Duplicate (word_id, direction, alternate) inserts collapse to one row."""
    conn, word = db_with_word
    for _ in range(2):
        conn.execute(
            """
            INSERT OR IGNORE INTO word_alternates
              (word_id, direction, alternate, source)
            VALUES (?, 'ja2en', 'fruit', 'gemini')
            """,
            (word.id,),
        )
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM word_alternates WHERE word_id = ? AND alternate = 'fruit'",
        (word.id,),
    ).fetchone()["n"]
    assert n == 1


def test_alternate_match_kana_direction(db_with_word) -> None:
    """en2ja path: normalized kana alternate should accept the user's kana input."""
    conn, word = db_with_word
    conn.execute(
        """
        INSERT INTO word_alternates (word_id, direction, alternate, source)
        VALUES (?, 'en2ja', 'アップル', 'gemini')
        """,
        (word.id,),
    )
    assert grade_typed("アップル", word, "en2ja", conn=conn) is True


def _post_typed_answer(client: TestClient, word_id: int, direction: str, typed: str) -> dict:
    return client.post(
        "/api/session/answer",
        json={
            "word_id": word_id,
            "direction": direction,
            "timed_out": False,
            "typed_answer": typed,
            "latency_ms": 1500,
        },
    ).json()


def test_semantic_grader_correct_persists_alternates(
    loaded_client: TestClient, db_path: Path
) -> None:
    """A wrong answer that Gemini accepts as 'correct' should:
       (a) flip the response to correct=True with feedback,
       (b) leave a row in word_alternates so the next try short-circuits."""
    from kana_quiz import gemini

    # Force the picked card into type-in mode.
    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]

    import sqlite3 as _sqlite

    conn = _sqlite.connect(db_path)
    now_iso = "2020-01-01T00:00:00+00:00"
    rows = conn.execute("SELECT id FROM words").fetchall()
    for r in rows:
        for direction_seed in ("en2ja", "ja2en"):
            conn.execute(
                """
                INSERT OR IGNORE INTO task_state
                  (word_id, task, ease, interval_days, repetitions,
                   due_at, introduced_at)
                VALUES (?, ?, 2.5, 0, 2, ?, ?)
                """,
                (r[0], direction_seed, now_iso, now_iso),
            )
    conn.execute(
        "UPDATE task_state SET repetitions = 2, ease = 2.5, due_at = '2020-01-01T00:00:00+00:00' "
        "WHERE word_id = ?",
        (word_id,),
    )
    conn.execute(
        "UPDATE task_state SET due_at = '2999-01-01T00:00:00+00:00' WHERE word_id != ?",
        (word_id,),
    )
    conn.commit()
    conn.close()

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "type"
    direction = nxt["direction"]

    fake_alt = "アップル" if direction == "en2ja" else "fruit"
    fake = gemini.SemanticGrade(
        verdict="correct",
        explanation="X means raise; Y means wake up",
        alternates=(fake_alt,),
    )

    with patch.object(gemini, "grade_semantic", return_value=fake):
        body = _post_typed_answer(
            loaded_client, word_id, direction, "definitely-not-the-answer-zzz"
        )

    assert body["correct"] is True
    assert body["outcome"] == "correct"
    assert body["feedback"] == "X means raise; Y means wake up"

    conn = _sqlite.connect(db_path)
    conn.row_factory = _sqlite.Row
    rows = conn.execute(
        "SELECT alternate FROM word_alternates WHERE word_id = ? AND direction = ?",
        (word_id, direction),
    ).fetchall()
    conn.close()
    alts = [r["alternate"] for r in rows]
    assert fake_alt in alts


def _seed_typeable_card(conn_path: Path, word_id: int) -> None:
    """Force ``word_id`` into type-in mode and park every other card so the
    next /session/next call returns a deterministic question."""
    import sqlite3 as _sqlite

    conn = _sqlite.connect(conn_path)
    now_iso = "2020-01-01T00:00:00+00:00"
    rows = conn.execute("SELECT id FROM words").fetchall()
    for r in rows:
        for direction_seed in ("en2ja", "ja2en"):
            conn.execute(
                """
                INSERT OR IGNORE INTO task_state
                  (word_id, task, ease, interval_days, repetitions,
                   due_at, introduced_at)
                VALUES (?, ?, 2.5, 0, 2, ?, ?)
                """,
                (r[0], direction_seed, now_iso, now_iso),
            )
    conn.execute(
        "UPDATE task_state SET repetitions = 2, ease = 2.5, "
        "due_at = '2020-01-01T00:00:00+00:00' WHERE word_id = ?",
        (word_id,),
    )
    conn.execute(
        "UPDATE task_state SET due_at = '2999-01-01T00:00:00+00:00' "
        "WHERE word_id != ?",
        (word_id,),
    )
    conn.commit()
    conn.close()


def test_semantic_grader_incorrect_surfaces_feedback(
    loaded_client: TestClient, db_path: Path
) -> None:
    """``incorrect`` verdict surfaces feedback but does NOT flip the card to
    correct and does NOT persist an alternate."""
    from kana_quiz import gemini

    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]
    _seed_typeable_card(db_path, word_id)

    nxt = loaded_client.get("/api/session/next").json()
    direction = nxt["direction"]

    fake = gemini.SemanticGrade(
        verdict="incorrect",
        explanation="Target was 起きる (to wake up); you typed an unrelated word.",
        alternates=(),
    )
    with patch.object(gemini, "grade_semantic", return_value=fake):
        body = _post_typed_answer(loaded_client, word_id, direction, "zzz-wrong")

    assert body["correct"] is False
    assert body["outcome"] == "incorrect"
    assert body["feedback"] == fake.explanation

    import sqlite3 as _sqlite

    conn = _sqlite.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM word_alternates WHERE word_id = ?", (word_id,)
    ).fetchone()[0]
    conn.close()
    assert n == 0


def test_semantic_grader_accept_passes_without_alternate(
    loaded_client: TestClient, db_path: Path
) -> None:
    """``accept`` passes the card (correct=True) and surfaces feedback, but
    does NOT save the student's answer as an alternate — they typed a
    different but related word, not a synonym of the target."""
    from kana_quiz import gemini

    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]
    _seed_typeable_card(db_path, word_id)

    nxt = loaded_client.get("/api/session/next").json()
    direction = nxt["direction"]

    fake = gemini.SemanticGrade(
        verdict="accept",
        explanation="Card targets 役立つ; you typed 役に立つ — both mean 'to be useful'.",
        alternates=(),  # accept never carries alternates
    )
    with patch.object(gemini, "grade_semantic", return_value=fake):
        body = _post_typed_answer(loaded_client, word_id, direction, "zzz-different")

    assert body["correct"] is True
    assert body["outcome"] == "correct"
    assert body["feedback"] == fake.explanation

    import sqlite3 as _sqlite

    conn = _sqlite.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM word_alternates WHERE word_id = ?", (word_id,)
    ).fetchone()[0]
    conn.close()
    assert n == 0
