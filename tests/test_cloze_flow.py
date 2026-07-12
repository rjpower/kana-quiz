"""Cloze rung — contextual production track.

Cloze surfaces once en→ja recall has cleared its unlock threshold AND a usable
cached sentence exists. It then runs on its own ``task_state`` SRS lane so
contextual-production misses do not downgrade basic word recall.
"""

from __future__ import annotations

import sqlite3 as _sqlite
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


def _seed_only_card(
    db: Path,
    word_id: int,
    ease: float,
    reps: int,
    *,
    mastery: dict[str, bool] | None = None,
) -> None:
    """Stamp every word as introduced; lift one card to the supplied SRS
    state and push every other due date into the far future so the
    picker is forced to surface our chosen word.

    ``mastery`` parks a cleared SRS row for each supplemental task mapped to a
    truthy value (e.g. ``{"cloze_choice": True}``). The type-in cloze lane now
    unlocks from selection-cloze mastery rather than directly from recall, so
    tests targeting ``mode='cloze'`` seed ``{"cloze_choice": True}``. Each
    parked row's due date is far-future so its own interleaver never picks it —
    it exists only to satisfy the unlock gate.
    """
    conn = _sqlite.connect(db)
    now_iso = "2020-01-01T00:00:00+00:00"
    future_iso = "2999-01-01T00:00:00+00:00"
    rows = conn.execute("SELECT id FROM words").fetchall()
    for (wid,) in rows:
        for d in ("en2ja", "ja2en"):
            conn.execute(
                """
                INSERT OR IGNORE INTO task_state
                  (word_id, task, ease, interval_days, repetitions,
                   due_at, introduced_at)
                VALUES (?, ?, 2.5, 0, 1, ?, ?)
                """,
                (wid, d, now_iso, now_iso),
            )
    conn.execute(
        "UPDATE task_state SET ease = ?, repetitions = ?, "
        "due_at = '2020-01-01T00:00:00+00:00' WHERE word_id = ? AND task = 'en2ja'",
        (ease, reps, word_id),
    )
    # ja2en card for the target stays at default so the picker has only
    # one viable en2ja candidate. Other cards parked.
    conn.execute(
        "UPDATE task_state SET due_at = '2999-01-01T00:00:00+00:00' "
        "WHERE word_id != ? OR task = 'ja2en'",
        (word_id,),
    )
    for task, mastered in (mastery or {}).items():
        if not mastered:
            continue
        conn.execute(
            """
            INSERT INTO task_state
              (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
            VALUES (?, ?, 2.9, 0, 3, ?, ?)
            ON CONFLICT(word_id, task) DO UPDATE SET
              ease = excluded.ease,
              repetitions = excluded.repetitions,
              due_at = excluded.due_at,
              introduced_at = excluded.introduced_at
            """,
            (word_id, task, future_iso, now_iso),
        )
    conn.commit()
    conn.close()


def _seed_sentence(db: Path, word_id: int, japanese: str, target_form: str) -> None:
    conn = _sqlite.connect(db)
    # Insert OR replace so successive callers can refresh the row.
    conn.execute(
        """
        INSERT INTO sentence_cache
          (word_id, model, japanese, english, mnemonic, target_form, created_at)
        VALUES (?, 'test-model', ?, 'translation', '', ?, '2020-01-01T00:00:00+00:00')
        ON CONFLICT(word_id, model) DO UPDATE SET
          japanese = excluded.japanese,
          target_form = excluded.target_form
        """,
        (word_id, japanese, target_form),
    )
    conn.commit()
    conn.close()


def _update_word(
    db: Path, word_id: int, *, kanji: str, kana: str, english: str
) -> None:
    conn = _sqlite.connect(db)
    conn.execute(
        "UPDATE words SET kanji = ?, kana = ?, english = ? WHERE id = ?",
        (kanji, kana, english, word_id),
    )
    conn.commit()
    conn.close()


def _state_row(db: Path, table: str, word_id: int, task: str | None = None) -> _sqlite.Row | None:
    conn = _sqlite.connect(db)
    conn.row_factory = _sqlite.Row
    try:
        if table == "task_state" and task is not None:
            return conn.execute(
                "SELECT * FROM task_state WHERE word_id = ? AND task = ?",
                (word_id, task),
            ).fetchone()
        return conn.execute(
            f"SELECT * FROM {table} WHERE word_id = ?", (word_id,)
        ).fetchone()
    finally:
        conn.close()


def _post_typed(client: TestClient, q: dict, typed: str) -> dict:
    """Mirror what the frontend store sends on a typed cloze submission."""
    return client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "timed_out": False,
            "typed_answer": typed,
            "mode": q["mode"],
            "cloze_expected": q.get("cloze_expected"),
            "latency_ms": 1500,
        },
    ).json()


def _force_test_model(monkeypatch) -> None:
    """Point the route's cloze lookup at our test cache row.

    The route looks up sentence_cache by gemini.DEFAULT_MODEL; tests
    seed the cache under a stable 'test-model' so a config-drift
    elsewhere doesn't shift what we're testing against.
    """
    from kana_quiz import gemini
    from kana_quiz.routes import session as routes_session
    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "test-model")
    monkeypatch.setattr(routes_session, "CLOZE_INTERLEAVE_PROBABILITY", 1.0)
    # Pin the selection-cloze interleaver off so it never preempts the type-in
    # cloze lane these tests are targeting.
    monkeypatch.setattr(routes_session, "CLOZE_CHOICE_INTERLEAVE_PROBABILITY", 0.0)


def test_migration_013_drops_cloze_state(
    loaded_client: TestClient, db_path: Path
) -> None:
    """init_schema + migrations consolidate cloze SRS into task_state."""
    conn = _sqlite.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cloze_state)")}
    conn.close()
    assert cols == set()


def test_migration_011_creates_task_state(
    loaded_client: TestClient, db_path: Path
) -> None:
    """init_schema + migrations leave task_state with unified task rows."""
    conn = _sqlite.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_state)")}
    conn.close()
    assert cols == {
        "word_id", "task", "ease", "interval_days", "repetitions",
        "due_at", "introduced_at",
    }


def test_promotion_to_cloze_when_threshold_clears(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """Card with mature recall + a populated sentence row
    surfaces as mode='cloze' with a {blank}-bearing template."""
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="役立つ", kana="やくだつ", english="useful")
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path, word_id,
        japanese="この本はとても役立ちます。",
        target_form="役立ち",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["word_id"] == word_id, nxt
    assert nxt["direction"] == "en2ja"
    assert nxt["mode"] == "cloze", nxt
    assert nxt["cloze_template"] == "この本はとても{blank}ます。"
    assert nxt["cloze_expected"] == "役立ち"


def test_cloze_template_blanks_full_na_adjective_form(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """Cached forms like 奇妙な are blanked in full — the な is hidden too.

    Previously the blank stopped at the lexical head (奇妙) and left な
    visible; that contradicted what the grader expected (奇妙な) and
    confused users into typing just the stem.
    """
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="奇妙", kana="きみょう", english="strange")
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path, word_id,
        japanese="昨日の夜、外で奇妙な音が聞こえました。",
        target_form="奇妙な",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze", nxt
    assert nxt["cloze_template"] == "昨日の夜、外で{blank}音が聞こえました。"
    assert nxt["cloze_expected"] == "奇妙な"


def test_cloze_template_blanks_full_polite_form(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """Polite conjugated target_form (役立ちます) is blanked whole, not split."""
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="役立つ", kana="やくだつ", english="useful")
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path, word_id,
        japanese="この本はとても役立ちます。",
        target_form="役立ちます",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze", nxt
    assert nxt["cloze_template"] == "この本はとても{blank}。"
    assert nxt["cloze_expected"] == "役立ちます"


def test_cloze_template_blanks_full_de_imasu_form(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """Progressive forms (望んでいます) are blanked whole, with no visible suffix."""
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="望む", kana="のぞむ", english="to hope")
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path, word_id,
        japanese="私は世界が平和になることを望んでいます。",
        target_form="望んでいます",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze", nxt
    assert nxt["cloze_template"] == "私は世界が平和になることを{blank}。"
    assert nxt["cloze_expected"] == "望んでいます"


def test_cloze_prefetch_excludes_current_word(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """The background next-card prefetch must not cache the live cloze card."""
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="役立つ", kana="やくだつ", english="useful")
    _seed_only_card(db_path, word_id, ease=2.7, reps=4, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path, word_id,
        japanese="この本はとても役立ちます。",
        target_form="役立ち",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze"
    prefetched = loaded_client.get(f"/api/session/next?exclude={word_id}")
    if prefetched.status_code == 204:
        return
    body = prefetched.json()
    assert body["word_id"] != word_id


def test_cloze_interleaver_can_yield_to_recall(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """Cloze should not monopolize /next when the interleave dice miss."""
    from kana_quiz import gemini
    from kana_quiz.routes import session as routes_session

    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "test-model")
    monkeypatch.setattr(routes_session, "CLOZE_INTERLEAVE_PROBABILITY", 0.35)
    monkeypatch.setattr(routes_session.random, "random", lambda: 0.99)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="役立つ", kana="やくだつ", english="useful")
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path, word_id,
        japanese="この本はとても役立ちます。",
        target_form="役立ち",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] != "cloze", nxt


def test_cloze_recovers_when_target_form_points_at_wrong_span(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """A stored target_form that happens to be a substring of the
    sentence but doesn't overlap the actual word should be ignored —
    locate_target_form() finds the right span instead.

    Regression: Gemini once cached target_form='日' for word 課 in the
    sentence '今日は教科書の第五課を勉強します。'. '日' IS in the sentence
    (inside 今日), so the naive substring check passed and the blank
    rendered at 今[__]は instead of 第五[__]を.
    """
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="課", kana="か", english="lesson, chapter")
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path, word_id,
        japanese="今日は教科書の第五課を勉強します。",
        target_form="日",  # wrong span — substring of 今日, not the target word
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze", nxt
    assert nxt["cloze_expected"] == "課", nxt
    assert nxt["cloze_template"] == "今日は教科書の第五{blank}を勉強します。", nxt


def test_cloze_falls_back_when_target_form_missing(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """A pre-cloze sentence row (target_form='') downgrades to type-in
    rather than handing back an unrenderable cloze template."""
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path, word_id,
        japanese="これはサンプルです。",
        target_form="",  # pre-cloze cache row
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["word_id"] == word_id
    assert nxt["mode"] == "type", nxt
    assert nxt.get("cloze_template") is None
    assert nxt.get("cloze_expected") is None


def test_cloze_falls_back_below_threshold(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """Cards that have graduated to type but haven't cleared the cloze unlock
    bar stay in type even if a sentence with target_form is cached."""
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="役立つ", kana="やくだつ", english="useful")
    # Reps cleared the type-in floor (>=1), but ease is still below the cloze
    # unlock bar.
    _seed_only_card(db_path, word_id, ease=2.55, reps=3)
    _seed_sentence(
        db_path, word_id,
        japanese="このアプリは役立ちます。",
        target_form="役立ち",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["word_id"] == word_id
    assert nxt["mode"] == "type", nxt


def test_cloze_grades_conjugated_form_correct(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """Typing the conjugated surface form (passed back via
    cloze_expected) marks correct even when the fuzzy ratio to the
    dictionary kana drops below 80%."""
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    # Coerce the picked card to a kana-only compound so the seeded
    # conjugation/sentence are consistent with the underlying word.
    _update_word(
        db_path, word_id, kanji="", kana="さかなりょうり", english="fish dish",
    )
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    # Put the kana 5 chars from the dictionary form so plain fuzzy at
    # 80% would NOT match — the cloze grader still has to accept it.
    _seed_sentence(
        db_path, word_id,
        japanese="きょうはさかなりょうりをたべます。",
        target_form="さかなりょうり",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze"
    body = _post_typed(loaded_client, nxt, "さかなりょうり")
    assert body["correct"] is True, body


def test_cloze_grades_dictionary_form_correct(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """Typing the dictionary form (target.kana) still marks correct —
    the user doesn't have to know the conjugation."""
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    # The dictionary form of the targeted word.
    word_row = _sqlite.connect(db_path).execute(
        "SELECT kana FROM words WHERE id = ?", (word_id,)
    ).fetchone()
    kana = word_row[0]
    _seed_sentence(
        db_path, word_id,
        japanese=f"きょうは{kana}をみました。",
        target_form=kana,
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze"
    body = _post_typed(loaded_client, nxt, kana)
    assert body["correct"] is True, body


def test_cloze_kana_surface_form_does_not_call_gemini(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """扱ってください should deterministically accept あつかって."""
    from kana_quiz import gemini

    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="扱う", kana="あつかう", english="to handle")
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path, word_id,
        japanese="この壊れやすい箱は、丁寧に扱ってください。",
        target_form="扱ってください",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze"

    def boom(*args, **kwargs):
        raise AssertionError("grade_semantic must not be called")

    with patch.object(gemini, "grade_semantic", side_effect=boom):
        body = _post_typed(loaded_client, nxt, "あつかって")
    assert body["correct"] is True, body


def test_cloze_gemini_alternate_short_circuits_next_attempt(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """Gemini-blessed cloze forms are cached under the cloze task."""
    from kana_quiz import gemini

    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="役立つ", kana="やくだつ", english="useful")
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path,
        word_id,
        japanese="この本はとても役立ちます。",
        target_form="役立ち",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze"
    fake = gemini.SemanticGrade(
        verdict="correct",
        explanation="alternate cloze form accepted",
        alternates=("べつのこたえ",),
    )
    with patch.object(gemini, "grade_semantic", return_value=fake):
        body = _post_typed(loaded_client, nxt, "not-a-kana-answer")
    assert body["correct"] is True

    conn = _sqlite.connect(db_path)
    conn.execute(
        "UPDATE task_state SET due_at = '2020-01-01T00:00:00+00:00' "
        "WHERE word_id = ? AND task = 'cloze'",
        (word_id,),
    )
    saved = conn.execute(
        "SELECT alternate FROM word_alternates "
        "WHERE word_id = ? AND direction = 'cloze'",
        (word_id,),
    ).fetchall()
    conn.commit()
    conn.close()
    assert saved == [("べつのこたえ",)]

    nxt2 = loaded_client.get("/api/session/next").json()
    assert nxt2["mode"] == "cloze"

    def boom(*args, **kwargs):
        raise AssertionError("grade_semantic must not be called on cache hit")

    with patch.object(gemini, "grade_semantic", side_effect=boom):
        body2 = _post_typed(loaded_client, nxt2, "べつのこたえ")
    assert body2["correct"] is True


def test_cloze_gemini_receives_sentence_context(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """Cloze fallback grading tells Gemini which surface form was blanked."""
    from kana_quiz import gemini

    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="役立つ", kana="やくだつ", english="useful")
    _seed_only_card(db_path, word_id, ease=2.9, reps=6, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path,
        word_id,
        japanese="この本はとても役立ちます。",
        target_form="役立ちます",
    )

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze"
    captured: list[gemini.ClozeGradeContext | None] = []

    def fake_grade(word, typed, direction, *, model=gemini.GRADER_MODEL, cloze_context=None):
        captured.append(cloze_context)
        return gemini.SemanticGrade(
            verdict="correct",
            explanation="conjugated cloze form accepted",
            alternates=("役立ちます",),
        )

    with patch.object(gemini, "grade_semantic", side_effect=fake_grade):
        body = _post_typed(loaded_client, nxt, "まちがい")

    assert body["correct"] is True
    assert captured == [
        gemini.ClozeGradeContext(
            sentence_japanese="この本はとても役立ちます。",
            target_form="役立ちます",
        )
    ]


def test_cloze_failure_updates_cloze_state_not_recall_state(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    """A contextual miss schedules the cloze lane without downgrading recall."""
    _force_test_model(monkeypatch)

    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _update_word(db_path, word_id, kanji="役立つ", kana="やくだつ", english="useful")
    _seed_only_card(db_path, word_id, ease=2.7, reps=4, mastery={"cloze_choice": True})
    _seed_sentence(
        db_path, word_id,
        japanese="このアプリは役立ちます。",
        target_form="役立ち",
    )

    before = _state_row(db_path, "task_state", word_id, "en2ja")
    assert before is not None

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["mode"] == "cloze"
    body = _post_typed(loaded_client, nxt, "まちがい")
    assert body["correct"] is False, body

    after = _state_row(db_path, "task_state", word_id, "en2ja")
    conn = _sqlite.connect(db_path)
    conn.row_factory = _sqlite.Row
    cloze = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = 'cloze'",
        (word_id,),
    ).fetchone()
    conn.close()
    assert after is not None
    assert cloze is not None
    assert after["ease"] == before["ease"]
    assert after["repetitions"] == before["repetitions"]
    assert cloze["ease"] < 2.5
    assert cloze["repetitions"] == 0

    conn = _sqlite.connect(db_path)
    try:
        review = conn.execute(
            "SELECT direction, correct FROM reviews WHERE word_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (word_id,),
        ).fetchone()
    finally:
        conn.close()
    assert review == ("cloze", 0)
