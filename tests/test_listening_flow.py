"""Listening / sentence-translation rung — Phase 2 of the progression.

The listening track is its own SRS lane (task `sentence_listen`) and
gets interleaved into the recall rotation at a low probability when an
eligible card exists. These tests pin down: the migration shape, the
eligibility gate (recall mastery + cached sentence + cached audio), the
interleaver dice, and the grade_translation routing on submit.
"""

from __future__ import annotations

import sqlite3 as _sqlite
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from kana_quiz import gemini
from kana_quiz.routes import session as routes_session


def _seed_audio(db: Path, text: str) -> None:
    """Drop a stub MP3 blob into audio_cache so the picker's audio-cache
    join finds a row for this sentence text.
    """
    conn = _sqlite.connect(db)
    conn.execute(
        """
        INSERT OR IGNORE INTO audio_cache
          (text, voice, model, mime, blob, created_at)
        VALUES (?, 'nova', 'gpt-4o-mini-tts', 'audio/mpeg', ?, '2020-01-01T00:00:00+00:00')
        """,
        (text, b"\x00\x01"),
    )
    conn.commit()
    conn.close()


def _seed_sentence(
    db: Path, word_id: int, japanese: str, english: str = "translation"
) -> None:
    conn = _sqlite.connect(db)
    conn.execute(
        """
        INSERT INTO sentence_cache
          (word_id, model, japanese, english, mnemonic, target_form, created_at)
        VALUES (?, 'test-model', ?, ?, '', '', '2020-01-01T00:00:00+00:00')
        ON CONFLICT(word_id, model) DO UPDATE SET
          japanese = excluded.japanese,
          english = excluded.english
        """,
        (word_id, japanese, english),
    )
    conn.commit()
    conn.close()


def _seed_task_state(
    db: Path, word_id: int, *, ease: float, reps: int,
    direction: str = "en2ja",
) -> None:
    conn = _sqlite.connect(db)
    now_iso = "2020-01-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO task_state
          (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
        VALUES (?, ?, ?, 0, ?, ?, ?)
        ON CONFLICT(word_id, task) DO UPDATE SET
          ease = excluded.ease,
          repetitions = excluded.repetitions
        """,
        (word_id, direction, ease, reps, now_iso, now_iso),
    )
    conn.commit()
    conn.close()


def test_migration_013_drops_listening_state(
    loaded_client: TestClient, db_path: Path
) -> None:
    """init_schema + migrations consolidate listening SRS into task_state."""
    conn = _sqlite.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listening_state)")}
    conn.close()
    assert cols == set()


def test_task_state_accepts_sentence_listen(
    loaded_client: TestClient, db_path: Path
) -> None:
    """init_schema + migrations allow the sentence_listen task id."""
    conn = _sqlite.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_state)")}
    conn.close()
    assert "task" in cols


def test_pick_listening_returns_none_without_eligible_word(
    loaded_client: TestClient, db_path: Path
) -> None:
    """Sample deck on its own — no recall task above the floor, no sentence
    cache, no audio — so the picker returns None."""
    from kana_quiz.db import connect
    from kana_quiz.session import pick_listening_card

    conn = connect()
    try:
        assert pick_listening_card(conn) is None
    finally:
        conn.close()


def test_pick_listening_eligibility_threshold(
    loaded_client: TestClient, db_path: Path
) -> None:
    """ease=2.5, reps=2 qualifies; ease=2.6, reps=1 does not.

    Boundary check on the mastery floor — both axes must clear before a
    word is allowed onto the listening track.
    """
    from kana_quiz.db import connect
    from kana_quiz.session import pick_listening_card

    q0 = loaded_client.get("/api/session/next").json()
    word_id_pass = q0["word_id"]
    # Find a different word for the failing case.
    conn = _sqlite.connect(db_path)
    other = conn.execute(
        "SELECT id FROM words WHERE id != ? ORDER BY id LIMIT 1",
        (word_id_pass,),
    ).fetchone()[0]
    conn.close()

    # Pass: ease at the floor, reps at the floor.
    _seed_task_state(db_path, word_id_pass, ease=2.5, reps=2)
    _seed_sentence(db_path, word_id_pass, japanese="これは犬です。")
    _seed_audio(db_path, "これは犬です。")

    # Fail: ease above the floor but reps below.
    _seed_task_state(db_path, other, ease=2.6, reps=1)
    _seed_sentence(db_path, other, japanese="これは猫です。")
    _seed_audio(db_path, "これは猫です。")

    conn = connect()
    try:
        pick = pick_listening_card(conn)
    finally:
        conn.close()
    assert pick is not None
    assert pick.word.id == word_id_pass
    assert pick.sentence_ja == "これは犬です。"


def test_pick_listening_skips_when_audio_missing(
    loaded_client: TestClient, db_path: Path
) -> None:
    """Recall-mastered word with a cached sentence but NO audio row
    must NOT surface — listening cards depend on audio being ready."""
    from kana_quiz.db import connect
    from kana_quiz.session import pick_listening_card

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _seed_task_state(db_path, word_id, ease=2.8, reps=4)
    _seed_sentence(db_path, word_id, japanese="これはテスト文です。")
    # Intentionally no _seed_audio.

    conn = connect()
    try:
        assert pick_listening_card(conn) is None
    finally:
        conn.close()


def test_interleaver_returns_listening_when_dice_under_threshold(
    loaded_client: TestClient, db_path: Path, monkeypatch,
) -> None:
    """When a listening card is eligible AND random.random() returns
    below LISTENING_INTERLEAVE_PROBABILITY, /session/next returns the
    listening question instead of the recall pick."""
    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _seed_task_state(db_path, word_id, ease=2.8, reps=4)
    _seed_sentence(
        db_path, word_id, japanese="これは犬です。",
        english="This is a dog.",
    )
    _seed_audio(db_path, "これは犬です。")

    # Force the dice to fall below the 0.2 threshold.
    monkeypatch.setattr(routes_session.random, "random", lambda: 0.1)

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "sentence_listen", nxt
    assert nxt["direction"] == "ja2en"
    assert nxt["audio_url"].startswith(f"/api/audio/sentence/{word_id}?s=")
    assert nxt["expected_translation"] == "This is a dog."
    assert nxt["sentence_japanese"] == "これは犬です。"
    assert nxt["prompt"] == ""
    # Time limit clamps to the listening window, not the type window.
    assert 12000 <= nxt["time_limit_ms"] <= 30000


def test_listening_prefetch_excludes_current_word(
    loaded_client: TestClient, db_path: Path, monkeypatch,
) -> None:
    """The background next-card prefetch must not cache the live listening card."""
    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _seed_task_state(db_path, word_id, ease=2.8, reps=4)
    _seed_sentence(
        db_path, word_id, japanese="これは犬です。",
        english="This is a dog.",
    )
    _seed_audio(db_path, "これは犬です。")
    monkeypatch.setattr(routes_session.random, "random", lambda: 0.0)

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "sentence_listen", nxt
    prefetched = loaded_client.get(f"/api/session/next?exclude={word_id}")
    if prefetched.status_code == 204:
        return
    body = prefetched.json()
    assert body["word_id"] != word_id


def test_interleaver_skips_listening_when_dice_above_threshold(
    loaded_client: TestClient, db_path: Path, monkeypatch,
) -> None:
    """When the dice land >= probability, the listening card stays
    parked and the recall picker fires."""
    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _seed_task_state(db_path, word_id, ease=2.8, reps=4)
    _seed_sentence(db_path, word_id, japanese="これは犬です。")
    _seed_audio(db_path, "これは犬です。")

    monkeypatch.setattr(routes_session.random, "random", lambda: 0.5)

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] != "sentence_listen", nxt


def _post_listen(
    client: TestClient, q: dict, typed: str, *, timed_out: bool = False
) -> dict:
    return client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "timed_out": timed_out,
            "typed_answer": typed,
            "mode": q["mode"],
            "latency_ms": 5000,
        },
    ).json()


def _serve_listening_card(
    client: TestClient, db_path: Path, monkeypatch, *, english: str
) -> dict:
    """Drive a single listening card to the wire and return the JSON."""
    q0 = client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _seed_task_state(db_path, word_id, ease=2.8, reps=4)
    _seed_sentence(
        db_path, word_id, japanese="これは犬です。", english=english,
    )
    _seed_audio(db_path, "これは犬です。")
    monkeypatch.setattr(routes_session.random, "random", lambda: 0.0)
    nxt = client.get("/api/session/next").json()
    assert nxt["mode"] == "sentence_listen", nxt
    return nxt


def test_listening_correct_updates_state(
    loaded_client: TestClient, db_path: Path, monkeypatch,
) -> None:
    """A 'correct' verdict promotes the sentence_listen task row: reps=1,
    interval_days==1, ease unchanged or bumped, due_at advanced."""
    nxt = _serve_listening_card(
        loaded_client, db_path, monkeypatch, english="This is a dog.",
    )
    fake = gemini.SemanticGrade(
        verdict="correct", explanation="", alternates=(),
    )
    with patch.object(gemini, "grade_translation", return_value=fake):
        body = _post_listen(loaded_client, nxt, "totally different wording")
    assert body["correct"] is True
    assert body["outcome"] == "correct"
    assert body["expected"] == "This is a dog."

    conn = _sqlite.connect(db_path)
    conn.row_factory = _sqlite.Row
    row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = 'sentence_listen'",
        (nxt["word_id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["repetitions"] == 1
    assert row["interval_days"] >= 1.0


def test_listening_accept_passes_with_feedback(
    loaded_client: TestClient, db_path: Path, monkeypatch,
) -> None:
    """'accept' passes the card (correct=True) and surfaces feedback;
    sentence_listen task state still increments."""
    nxt = _serve_listening_card(
        loaded_client, db_path, monkeypatch, english="This is a dog.",
    )
    fake = gemini.SemanticGrade(
        verdict="accept",
        explanation="You dropped 'this'; reference was 'This is a dog.'",
        alternates=(),
    )
    with patch.object(gemini, "grade_translation", return_value=fake):
        body = _post_listen(loaded_client, nxt, "is dog")
    assert body["correct"] is True
    assert body["feedback"] == fake.explanation

    conn = _sqlite.connect(db_path)
    conn.row_factory = _sqlite.Row
    row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = 'sentence_listen'",
        (nxt["word_id"],),
    ).fetchone()
    conn.close()
    assert row["repetitions"] == 1


def test_listening_late_typed_answer_can_still_pass(
    loaded_client: TestClient, db_path: Path, monkeypatch,
) -> None:
    """A timeout flag should not override a submitted translation.

    This protects older clients and keeps speed as an ease signal rather than
    the primary correctness criterion for full-sentence translation.
    """
    nxt = _serve_listening_card(
        loaded_client, db_path, monkeypatch, english="This is a dog.",
    )
    fake = gemini.SemanticGrade(
        verdict="correct", explanation="", alternates=(),
    )
    with patch.object(gemini, "grade_translation", return_value=fake):
        body = _post_listen(
            loaded_client, nxt, "eventually correct wording", timed_out=True,
        )
    assert body["correct"] is True
    assert body["outcome"] == "correct"


def test_listening_incorrect_resets_and_drops_ease(
    loaded_client: TestClient, db_path: Path, monkeypatch,
) -> None:
    """Wrong answer drops ease and resets repetitions to 0."""
    nxt = _serve_listening_card(
        loaded_client, db_path, monkeypatch, english="This is a dog.",
    )
    fake = gemini.SemanticGrade(
        verdict="incorrect",
        explanation="Reference was 'This is a dog.'",
        alternates=(),
    )
    with patch.object(gemini, "grade_translation", return_value=fake):
        body = _post_listen(loaded_client, nxt, "the moon is bright tonight")
    assert body["correct"] is False
    assert body["outcome"] == "incorrect"
    assert body["feedback"] == fake.explanation

    conn = _sqlite.connect(db_path)
    conn.row_factory = _sqlite.Row
    row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = 'sentence_listen'",
        (nxt["word_id"],),
    ).fetchone()
    conn.close()
    # Default ease is 2.5; an incorrect answer applies INCORRECT_PENALTY (0.15).
    assert row["ease"] < 2.5
    assert row["repetitions"] == 0


def test_grade_translation_fuzzy_short_circuits_without_client(monkeypatch) -> None:
    """An exact / near-exact typed answer returns 'correct' via the
    fuzzy pre-check WITHOUT instantiating a Gemini client.

    We force ``_get_client`` to blow up — if the function returns
    'correct' anyway, the fuzzy bypass fired before the client call,
    which is the whole point of that bypass for cost control.
    """
    def boom() -> object:
        raise AssertionError("client must not be instantiated on fuzzy hit")

    monkeypatch.setattr(gemini, "_get_client", boom)
    grade = gemini.grade_translation(
        "これは犬です。", "This is a dog.", "This is a dog.",
    )
    assert grade.verdict == "correct"
    assert grade.alternates == ()
    assert grade.clarified_gloss is None


def test_listening_correct_caches_alternates_and_short_circuits_repeat(
    loaded_client: TestClient, db_path: Path, monkeypatch,
) -> None:
    """On a 'correct' verdict the grader's curated alternates get saved
    into word_alternates(direction='sentence_listen'). A subsequent identical
    submission must NOT call grade_translation again — the cache hit
    short-circuits before Gemini.
    """
    nxt = _serve_listening_card(
        loaded_client, db_path, monkeypatch, english="Thank you.",
    )
    fake = gemini.SemanticGrade(
        verdict="correct",
        explanation="",
        alternates=("thank you", "thanks"),
    )
    with patch.object(gemini, "grade_translation", return_value=fake) as g:
        body = _post_listen(loaded_client, nxt, "thanks")
        assert body["correct"] is True
        assert g.call_count == 1

    # Alternates persisted under direction='sentence_listen'.
    conn = _sqlite.connect(db_path)
    rows = conn.execute(
        "SELECT alternate FROM word_alternates WHERE word_id = ? AND direction = ?",
        (nxt["word_id"], "sentence_listen"),
    ).fetchall()
    conn.close()
    cached = {r[0] for r in rows}
    assert {"thank you", "thanks"}.issubset(cached)

    # Serve the same listening card again; this time the cache should
    # absorb the answer and grade_translation must not be called.
    # The previous correct verdict pushed sentence_listen due_at into
    # the future — reset it so the picker re-surfaces the card.
    conn = _sqlite.connect(db_path)
    conn.execute(
        "UPDATE task_state SET due_at = '2020-01-01T00:00:00+00:00' WHERE word_id = ? AND task = 'sentence_listen'",
        (nxt["word_id"],),
    )
    conn.commit()
    conn.close()
    _seed_task_state(db_path, nxt["word_id"], ease=2.8, reps=4)
    monkeypatch.setattr(routes_session.random, "random", lambda: 0.0)
    nxt2 = loaded_client.get("/api/session/next").json()
    assert nxt2["mode"] == "sentence_listen"

    def boom(*_a, **_k):
        raise AssertionError("grade_translation must not be called on cache hit")

    with patch.object(gemini, "grade_translation", side_effect=boom):
        body2 = _post_listen(loaded_client, nxt2, "thanks")
    assert body2["correct"] is True


def test_listening_incorrect_does_not_cache_alternates(
    loaded_client: TestClient, db_path: Path, monkeypatch,
) -> None:
    """An 'incorrect' verdict must not pollute word_alternates — even
    if the grader returned a non-empty list, the caller drops it."""
    nxt = _serve_listening_card(
        loaded_client, db_path, monkeypatch, english="This is a dog.",
    )
    # A buggy grader could conceivably return alternates on incorrect;
    # the caller guards against polluting the cache regardless.
    fake = gemini.SemanticGrade(
        verdict="incorrect",
        explanation="Reference was 'This is a dog.'",
        alternates=("this is a dog",),
    )
    with patch.object(gemini, "grade_translation", return_value=fake):
        _post_listen(loaded_client, nxt, "the moon is bright")

    conn = _sqlite.connect(db_path)
    rows = conn.execute(
        "SELECT alternate FROM word_alternates WHERE word_id = ? AND direction = ?",
        (nxt["word_id"], "sentence_listen"),
    ).fetchall()
    conn.close()
    assert rows == []


def test_listening_endpoint_passes_typed_through_to_grader(
    loaded_client: TestClient, db_path: Path, monkeypatch,
) -> None:
    """The /session/answer route relays the typed answer + reference
    Japanese + reference English to grade_translation."""
    nxt = _serve_listening_card(
        loaded_client, db_path, monkeypatch, english="This is a dog.",
    )
    captured: dict[str, object] = {}

    def fake(japanese: str, ref_en: str, typed: str, **_):
        captured["japanese"] = japanese
        captured["ref_en"] = ref_en
        captured["typed"] = typed
        return gemini.SemanticGrade(
            verdict="incorrect", explanation="nope", alternates=(),
        )

    with patch.object(gemini, "grade_translation", side_effect=fake):
        _post_listen(loaded_client, nxt, "totally wrong answer here")

    assert captured["japanese"] == "これは犬です。"
    assert captured["ref_en"] == "This is a dog."
    assert captured["typed"] == "totally wrong answer here"
