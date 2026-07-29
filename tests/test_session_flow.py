"""End-to-end session flow: next question -> submit answer -> SRS updates."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient


def _word_by_id(db_path: Path, word_id: int) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    conn.close()
    return dict(row)


def _task_state(db_path: Path, word_id: int, direction: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = ?",
        (word_id, direction),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def _set_repetitions(
    db_path: Path, word_id: int, repetitions: int, direction: str = "en2ja"
) -> None:
    conn = sqlite3.connect(db_path)
    # Both recall tasks get bumped so the type-mode test isn't sensitive to
    # which direction the picker happens to land on.
    conn.execute(
        "UPDATE task_state SET repetitions = ? WHERE word_id = ? AND task IN ('en2ja', 'ja2en')",
        (repetitions, word_id),
    )
    conn.commit()
    conn.close()


def _set_task_state(
    db_path: Path,
    word_id: int,
    *,
    repetitions: int,
    ease: float,
) -> None:
    """Tune both directions of a card to a known (ease, repetitions) pair.

    The promotion rule is now ease-based, so tests need to drive both
    knobs to land in MC vs type-in deterministically regardless of which
    direction the picker chooses.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE task_state SET repetitions = ?, ease = ? WHERE word_id = ? AND task IN ('en2ja', 'ja2en')",
        (repetitions, ease, word_id),
    )
    conn.commit()
    conn.close()


def _force_only_due_word(db_path: Path, keep_id: int) -> None:
    """Make ``keep_id`` the only card /next can pick.

    Push every other card's due_at far into the future and ensure every
    word has recall task rows so the "pull a fresh card" branch doesn't
    fire.
    """
    conn = sqlite3.connect(db_path)
    now_iso = "2020-01-01T00:00:00+00:00"
    # Seed task_state for any word that doesn't have rows yet (mimics the
    # auto-introduction the picker would do).
    rows = conn.execute("SELECT id FROM words").fetchall()
    for r in rows:
        for direction in ("en2ja", "ja2en"):
            conn.execute(
                """
                INSERT OR IGNORE INTO task_state
                  (word_id, task, ease, interval_days, repetitions,
                   due_at, introduced_at)
                VALUES (?, ?, 2.5, 0, 0, ?, ?)
                """,
                (r[0], direction, now_iso, now_iso),
            )
    # OR IGNORE skips rows the picker already reserved, and those carry a
    # NULL introduced_at until the user answers. Row-exists is not
    # introduced-ness, so stamp them to really introduce every word.
    conn.execute(
        "UPDATE task_state SET introduced_at = ? WHERE introduced_at IS NULL",
        (now_iso,),
    )
    conn.execute(
        "UPDATE task_state SET due_at = '2999-01-01T00:00:00+00:00' WHERE word_id != ?",
        (keep_id,),
    )
    conn.execute(
        "UPDATE task_state SET due_at = ? WHERE word_id = ? AND task IN ('en2ja', 'ja2en')",
        (now_iso, keep_id),
    )
    conn.commit()
    conn.close()


def test_next_question_returns_4_choices(loaded_client: TestClient) -> None:
    resp = loaded_client.get("/api/session/next")
    assert resp.status_code == 200
    q = resp.json()
    assert q["mode"] == "mc"
    assert len(q["choices"]) == 4
    assert 0 <= q["correct_index"] < 4
    assert q["direction"] in {"en2ja", "ja2en"}
    assert len(set(q["choices"])) == 4  # no duplicate labels


def test_next_question_204_on_empty_deck(client: TestClient) -> None:
    resp = client.get("/api/session/next")
    assert resp.status_code == 204


def test_answering_correctly_pushes_due_at_into_future(
    loaded_client: TestClient, db_path: Path
) -> None:
    q = loaded_client.get("/api/session/next").json()
    before = _task_state(db_path, q["word_id"], q["direction"])

    resp = loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "chosen_index": q["correct_index"],
            "correct_index": q["correct_index"],
            "timed_out": False,
            "latency_ms": 1200,
        },
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["correct"] is True
    assert result["outcome"] == "correct"
    assert result["interval_days"] >= 1.0

    after = _task_state(db_path, q["word_id"], q["direction"])
    assert after["repetitions"] == before["repetitions"] + 1
    assert after["due_at"] > before["due_at"]


def test_wrong_answer_resets_repetitions(loaded_client: TestClient, db_path: Path) -> None:
    q = loaded_client.get("/api/session/next").json()
    wrong_index = (q["correct_index"] + 1) % 4

    resp = loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "chosen_index": wrong_index,
            "correct_index": q["correct_index"],
            "timed_out": False,
        },
    )
    body = resp.json()
    assert body["correct"] is False
    assert body["outcome"] == "incorrect"

    after = _task_state(db_path, q["word_id"], q["direction"])
    assert after["repetitions"] == 0
    assert after["interval_days"] < 1.0


def test_timeout_is_recorded_as_timeout(loaded_client: TestClient, db_path: Path) -> None:
    q = loaded_client.get("/api/session/next").json()
    resp = loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "chosen_index": None,
            "correct_index": q["correct_index"],
            "timed_out": True,
        },
    )
    assert resp.json()["outcome"] == "timeout"


def test_session_introduces_new_words_over_time(loaded_client: TestClient) -> None:
    # With 10 sample words, 20 correct answers should cover every word (fresh
    # ones introduced first) and then reuse the soonest-due word for the
    # remaining iterations — /next should never return 204 while any word
    # exists in the deck.
    introduced: set[int] = set()
    answers = 0
    for _ in range(20):
        q_resp = loaded_client.get("/api/session/next")
        assert q_resp.status_code == 200
        q = q_resp.json()
        introduced.add(q["word_id"])
        loaded_client.post(
            "/api/session/answer",
            json={
                "word_id": q["word_id"],
                "direction": q["direction"],
                "chosen_index": q["correct_index"],
                "correct_index": q["correct_index"],
                "timed_out": False,
            },
        )
        answers += 1

    assert len(introduced) == 10  # all sample words pulled into rotation
    stats = loaded_client.get("/api/stats").json()
    assert stats["introduced"] == 10
    assert stats["reviews_last_7_days"] == answers
    assert stats["accuracy_last_7_days"] == 1.0


def test_empty_deck_still_returns_204(client: TestClient) -> None:
    # Reuse fallback only applies once words have been introduced; a pristine
    # deck should keep returning 204 so the UI knows to prompt for import.
    resp = client.get("/api/session/next")
    assert resp.status_code == 204


def test_detailed_stats_histogram_and_maturity(loaded_client: TestClient) -> None:
    # Feed a couple of answers with explicit latencies so the histogram has
    # something to bucket.
    q1 = loaded_client.get("/api/session/next").json()
    loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": q1["word_id"],
            "direction": q1["direction"],
            "chosen_index": q1["correct_index"],
            "correct_index": q1["correct_index"],
            "timed_out": False,
            "latency_ms": 1200,
        },
    )
    q2 = loaded_client.get("/api/session/next").json()
    loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": q2["word_id"],
            "direction": q2["direction"],
            "chosen_index": q2["correct_index"],
            "correct_index": q2["correct_index"],
            "timed_out": False,
            "latency_ms": 3200,
        },
    )

    body = loaded_client.get("/api/stats/detailed").json()
    assert body["median_latency_ms"] in (1200, 3200)
    # Total histogram count should equal correct-answer count.
    total = sum(b["count"] for b in body["latency_histogram"])
    assert total == 2
    # One bar in the 1.0–1.5s bucket, one in the 3.0–3.5s bucket.
    by_lower = {b["lower_ms"]: b["count"] for b in body["latency_histogram"]}
    assert by_lower[1000] == 1
    assert by_lower[3000] == 1
    # Maturity counts sum to total words, and at least two words have been
    # introduced (so they're not all "new").
    mc = body["maturity_counts"]
    assert sum(mc.values()) == 10
    assert mc["new"] <= 8


def test_word_at_threshold_returns_type_mode(
    loaded_client: TestClient, db_path: Path
) -> None:
    # Pick a word, set ease >= 2.5 with at least one rep, force it to be
    # the only due word, then assert /next returns mode=type with no
    # choices.
    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]
    _set_task_state(db_path, word_id, repetitions=2, ease=2.5)
    _force_only_due_word(db_path, word_id)

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["word_id"] == word_id
    assert nxt["mode"] == "type"
    assert nxt["choices"] == []


def test_word_below_ease_threshold_returns_mc_mode(
    loaded_client: TestClient, db_path: Path
) -> None:
    # Even with multiple reps, an ease below 2.5 keeps the user in MC —
    # the card is shaky and needs more recognition practice.
    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]
    _set_task_state(db_path, word_id, repetitions=2, ease=2.4)
    _force_only_due_word(db_path, word_id)

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["word_id"] == word_id
    assert nxt["mode"] == "mc"
    assert len(nxt["choices"]) == 4


def test_word_with_zero_reps_returns_mc_mode(
    loaded_client: TestClient, db_path: Path
) -> None:
    # repetitions < 1 always routes to MC even if ease is at default 2.5.
    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]
    _set_task_state(db_path, word_id, repetitions=0, ease=2.5)
    _force_only_due_word(db_path, word_id)

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["word_id"] == word_id
    assert nxt["mode"] == "mc"


def test_typed_answer_grades_correctly(loaded_client: TestClient, db_path: Path) -> None:
    # Pick a sample word, force type mode, submit the exact kana.
    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]
    _set_task_state(db_path, word_id, repetitions=2, ease=2.5)
    _force_only_due_word(db_path, word_id)

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "type"
    target_kana = _word_by_id(db_path, word_id)["kana"]
    target_english = _word_by_id(db_path, word_id)["english"]

    typed = target_kana if nxt["direction"] == "en2ja" else target_english
    expected_field = target_kana if nxt["direction"] == "en2ja" else target_english

    resp = loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": word_id,
            "direction": nxt["direction"],
            "timed_out": False,
            "typed_answer": typed,
            "latency_ms": 1500,
        },
    )
    body = resp.json()
    assert body["correct"] is True
    assert body["outcome"] == "correct"
    assert body["expected"] == expected_field


def test_typed_answer_wrong_returns_expected(
    loaded_client: TestClient, db_path: Path
) -> None:
    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]
    _set_task_state(db_path, word_id, repetitions=2, ease=2.5)
    _force_only_due_word(db_path, word_id)
    nxt = loaded_client.get("/api/session/next").json()

    resp = loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": word_id,
            "direction": nxt["direction"],
            "timed_out": False,
            "typed_answer": "definitely-not-the-answer-zzz",
        },
    )
    body = resp.json()
    assert body["correct"] is False
    assert body["outcome"] == "incorrect"
    assert body["expected"]  # reveal the right answer


def test_consecutive_correct_answers_grow_interval(
    loaded_client: TestClient, db_path: Path
) -> None:
    # Answer the same word correctly multiple times and assert the interval grows.
    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]

    def answer(correct_index: int, direction: str) -> dict:
        return loaded_client.post(
            "/api/session/answer",
            json={
                "word_id": word_id,
                "direction": direction,
                "chosen_index": correct_index,
                "correct_index": correct_index,
                "timed_out": False,
            },
        ).json()

    first = answer(q["correct_index"], q["direction"])
    second = answer(0, q["direction"])  # indices don't matter since we set them equal
    third = answer(0, q["direction"])
    assert first["interval_days"] < second["interval_days"] < third["interval_days"]
