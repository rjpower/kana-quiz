"""Selection cloze rung — recognition-in-context.

``cloze_choice`` sits between plain multiple choice and the type-in cloze lane
on the difficulty ladder: the user picks the missing word from four kana
choices rather than typing it. It unlocks from recall mastery (taking over the
gate the type-in lane used to own), runs on its own ``task_state`` lane, and
mastering it is what now unlocks the type-in cloze lane.
"""

from __future__ import annotations

import sqlite3 as _sqlite
from pathlib import Path

from fastapi.testclient import TestClient


def _seed_only_card(db: Path, word_id: int, ease: float, reps: int) -> None:
    """Introduce every word; lift one card's en2ja recall to (ease, reps) and
    park every other due date so the picker is forced onto our chosen word."""
    conn = _sqlite.connect(db)
    now_iso = "2020-01-01T00:00:00+00:00"
    for (wid,) in conn.execute("SELECT id FROM words").fetchall():
        for d in ("en2ja", "ja2en"):
            conn.execute(
                """
                INSERT OR IGNORE INTO task_state
                  (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
                VALUES (?, ?, 2.5, 0, 1, ?, ?)
                """,
                (wid, d, now_iso, now_iso),
            )
    conn.execute(
        "UPDATE task_state SET ease = ?, repetitions = ?, due_at = ? "
        "WHERE word_id = ? AND task = 'en2ja'",
        (ease, reps, now_iso, word_id),
    )
    conn.execute(
        "UPDATE task_state SET due_at = '2999-01-01T00:00:00+00:00' "
        "WHERE word_id != ? OR task = 'ja2en'",
        (word_id,),
    )
    conn.commit()
    conn.close()


def _seed_sentence(db: Path, word_id: int, japanese: str, target_form: str) -> None:
    conn = _sqlite.connect(db)
    conn.execute(
        """
        INSERT INTO sentence_cache
          (word_id, model, japanese, english, mnemonic, target_form, created_at)
        VALUES (?, 'test-model', ?, 'translation', '', ?, '2020-01-01T00:00:00+00:00')
        ON CONFLICT(word_id, model) DO UPDATE SET
          japanese = excluded.japanese, target_form = excluded.target_form
        """,
        (word_id, japanese, target_form),
    )
    conn.commit()
    conn.close()


def _update_word(db: Path, word_id: int, *, kanji: str, kana: str, english: str) -> None:
    conn = _sqlite.connect(db)
    conn.execute(
        "UPDATE words SET kanji = ?, kana = ?, english = ? WHERE id = ?",
        (kanji, kana, english, word_id),
    )
    conn.commit()
    conn.close()


def _task_row(db: Path, word_id: int, task: str) -> _sqlite.Row | None:
    conn = _sqlite.connect(db)
    conn.row_factory = _sqlite.Row
    try:
        return conn.execute(
            "SELECT * FROM task_state WHERE word_id = ? AND task = ?",
            (word_id, task),
        ).fetchone()
    finally:
        conn.close()


def _force_selection_cloze(monkeypatch) -> None:
    """Pin the selection-cloze interleaver on and the type-in cloze lane off,
    so a card that qualifies for selection cloze surfaces deterministically."""
    from kana_quiz import gemini
    from kana_quiz.routes import session as routes_session
    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "test-model")
    monkeypatch.setattr(routes_session, "CLOZE_CHOICE_INTERLEAVE_PROBABILITY", 1.0)
    monkeypatch.setattr(routes_session, "CLOZE_INTERLEAVE_PROBABILITY", 0.0)


def _post_choice(client: TestClient, q: dict, chosen: int | None, *, timed_out: bool = False) -> dict:
    """Mirror what the store sends for a selection cloze submission."""
    return client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "chosen_index": chosen,
            "correct_index": q["correct_index"],
            "timed_out": timed_out,
            "mode": "cloze_choice",
            "latency_ms": 1500,
        },
    ).json()


def _setup_eligible(client: TestClient, db: Path) -> int:
    """Pull a card, make it a kanji verb with recall mastery + a cached
    sentence, and return its word_id. After this the word qualifies for the
    selection cloze lane but has no cloze_choice row yet."""
    word_id = client.get("/api/session/next").json()["word_id"]
    _update_word(db, word_id, kanji="役立つ", kana="やくだつ", english="useful")
    _seed_only_card(db, word_id, ease=2.9, reps=6)
    _seed_sentence(db, word_id, japanese="この本はとても役立ちます。", target_form="役立ち")
    return word_id


def test_selection_cloze_unlocks_from_recall_mastery(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """A recall-mastered card with a cached sentence surfaces as cloze_choice:
    a {blank} template plus four kana choices, one of which is the target."""
    _force_selection_cloze(monkeypatch)
    word_id = _setup_eligible(loaded_client, db_path)

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["word_id"] == word_id, nxt
    assert nxt["mode"] == "cloze_choice", nxt
    assert nxt["direction"] == "en2ja"
    assert nxt["cloze_template"] == "この本はとても{blank}ます。"
    assert len(nxt["choices"]) == 4
    assert len(set(nxt["choices"])) == 4  # four distinct labels — no coin-flip cell
    assert 0 <= nxt["correct_index"] < 4
    # The correct choice is the dictionary kana, not the conjugated surface
    # form — uniform script across all four so kanji can't give it away.
    assert nxt["choices"][nxt["correct_index"]] == "やくだつ"
    assert "役" not in "".join(nxt["choices"])
    # cloze_expected carries the surface form for the post-answer reveal.
    assert nxt["cloze_expected"] == "役立ち"


def test_selection_cloze_correct_records_on_its_own_lane(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """A correct pick advances the cloze_choice SRS row and leaves recall be."""
    _force_selection_cloze(monkeypatch)
    word_id = _setup_eligible(loaded_client, db_path)
    recall_before = _task_row(db_path, word_id, "en2ja")

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze_choice"
    body = _post_choice(loaded_client, nxt, nxt["correct_index"])
    assert body["correct"] is True, body
    assert body["outcome"] == "correct"

    cc = _task_row(db_path, word_id, "cloze_choice")
    recall_after = _task_row(db_path, word_id, "en2ja")
    assert cc is not None and cc["repetitions"] == 1
    assert recall_after["ease"] == recall_before["ease"]
    assert recall_after["repetitions"] == recall_before["repetitions"]

    conn = _sqlite.connect(db_path)
    try:
        review = conn.execute(
            "SELECT direction, correct FROM reviews WHERE word_id = ? ORDER BY id DESC LIMIT 1",
            (word_id,),
        ).fetchone()
    finally:
        conn.close()
    assert review == ("cloze_choice", 1)


def test_selection_cloze_wrong_does_not_downgrade_recall(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """A wrong pick schedules the cloze_choice lane without touching recall."""
    _force_selection_cloze(monkeypatch)
    word_id = _setup_eligible(loaded_client, db_path)
    recall_before = _task_row(db_path, word_id, "en2ja")

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze_choice"
    wrong = (nxt["correct_index"] + 1) % 4
    body = _post_choice(loaded_client, nxt, wrong)
    assert body["correct"] is False, body

    cc = _task_row(db_path, word_id, "cloze_choice")
    recall_after = _task_row(db_path, word_id, "en2ja")
    assert cc is not None and cc["repetitions"] == 0
    assert recall_after["ease"] == recall_before["ease"]
    assert recall_after["repetitions"] == recall_before["repetitions"]


def test_selection_cloze_timeout_is_recorded(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    _force_selection_cloze(monkeypatch)
    _setup_eligible(loaded_client, db_path)
    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze_choice"
    body = _post_choice(loaded_client, nxt, None, timed_out=True)
    assert body["correct"] is False
    assert body["outcome"] == "timeout"


def test_type_in_cloze_requires_selection_cloze_mastery(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """The ladder: recall mastery alone no longer unlocks the type-in cloze
    lane. With selection cloze disabled and type-in cloze forced, a mastered
    card with no cloze_choice progress stays on plain recall (type-in)."""
    from kana_quiz import gemini
    from kana_quiz.routes import session as routes_session
    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "test-model")
    monkeypatch.setattr(routes_session, "CLOZE_INTERLEAVE_PROBABILITY", 1.0)
    monkeypatch.setattr(routes_session, "CLOZE_CHOICE_INTERLEAVE_PROBABILITY", 0.0)

    _setup_eligible(loaded_client, db_path)
    nxt = loaded_client.get("/api/session/next").json()
    # No cloze_choice row exists, so the type-in cloze lane is not eligible
    # despite recall mastery + a cached sentence; recall serves type-in.
    assert nxt["mode"] == "type", nxt

    # Once selection cloze is mastered, the type-in lane unlocks.
    conn = _sqlite.connect(db_path)
    conn.execute(
        """
        INSERT INTO task_state
          (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
        VALUES (?, 'cloze_choice', 2.9, 0, 3, '2999-01-01T00:00:00+00:00',
                '2020-01-01T00:00:00+00:00')
        """,
        (nxt["word_id"],),
    )
    conn.commit()
    conn.close()

    promoted = loaded_client.get("/api/session/next").json()
    assert promoted["mode"] == "cloze", promoted


def test_selection_cloze_suppressed_by_modes_param(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """A cloze_choice-eligible card is not served as cloze_choice when the
    client's modes allow-list omits it."""
    _force_selection_cloze(monkeypatch)
    _setup_eligible(loaded_client, db_path)

    nxt = loaded_client.get("/api/session/next?modes=mc,type").json()
    assert nxt["mode"] != "cloze_choice", nxt
