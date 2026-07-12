"""Ignore/un-ignore endpoints + picker exclusion."""

from fastapi.testclient import TestClient


def _all_words(client: TestClient) -> list[dict]:
    # Drain the picker until 204 so we know which word IDs are reachable.
    seen: list[int] = []
    while True:
        resp = client.get("/api/session/next")
        if resp.status_code == 204:
            break
        q = resp.json()
        if q["word_id"] in seen:
            # Loop guard — the deck is small and cards repeat once exhausted.
            break
        seen.append(q["word_id"])
        client.post(
            "/api/session/answer",
            json={
                "word_id": q["word_id"],
                "direction": q["direction"],
                "chosen_index": q["correct_index"],
                "correct_index": q["correct_index"],
                "timed_out": False,
            },
        )
    return [{"id": i} for i in seen]


def test_ignore_excludes_from_picker(loaded_client: TestClient) -> None:
    # Pick the first available word and ignore it.
    q = loaded_client.get("/api/session/next").json()
    target_id = q["word_id"]

    resp = loaded_client.post(f"/api/words/{target_id}/ignore")
    assert resp.status_code == 204

    decks = loaded_client.get("/api/decks").json()
    deck = decks[0]
    assert deck["ignored_count"] == 1

    # Ignored words shouldn't appear via /api/session/next, even when we
    # exhaust the rest of the deck.
    seen: set[int] = set()
    for _ in range(50):
        resp = loaded_client.get("/api/session/next")
        if resp.status_code == 204:
            break
        wid = resp.json()["word_id"]
        if wid in seen:
            break
        seen.add(wid)
        client_q = resp.json()
        loaded_client.post(
            "/api/session/answer",
            json={
                "word_id": wid,
                "direction": client_q["direction"],
                "chosen_index": client_q["correct_index"],
                "correct_index": client_q["correct_index"],
                "timed_out": False,
            },
        )
    assert target_id not in seen


def test_drill_skips_ignored_word(loaded_client: TestClient) -> None:
    # A word the user missed and then ignored before the burndown must not be
    # drillable — the drill endpoint 404s it so the client's quick-fire loop
    # skips it rather than re-testing an ignored word.
    q = loaded_client.get("/api/session/next").json()
    target_id = q["word_id"]

    ok = loaded_client.get(f"/api/session/drill?word_id={target_id}&direction=ja2en")
    assert ok.status_code == 200  # drillable before ignoring

    assert loaded_client.post(f"/api/words/{target_id}/ignore").status_code == 204

    gone = loaded_client.get(f"/api/session/drill?word_id={target_id}&direction=ja2en")
    assert gone.status_code == 404


def test_unignore_re_enables(loaded_client: TestClient) -> None:
    q = loaded_client.get("/api/session/next").json()
    target_id = q["word_id"]
    loaded_client.post(f"/api/words/{target_id}/ignore")

    listing = loaded_client.get(f"/api/decks/{1}/ignored").json()
    assert any(w["id"] == target_id for w in listing)

    resp = loaded_client.delete(f"/api/words/{target_id}/ignore")
    assert resp.status_code == 204

    decks = loaded_client.get("/api/decks").json()
    assert decks[0]["ignored_count"] == 0
    listing = loaded_client.get(f"/api/decks/{1}/ignored").json()
    assert listing == []


def test_ignore_unknown_word_is_404(client: TestClient) -> None:
    assert client.post("/api/words/99999/ignore").status_code == 404
    assert client.delete("/api/words/99999/ignore").status_code == 404


def test_question_carries_time_limit_ms(loaded_client: TestClient) -> None:
    q = loaded_client.get("/api/session/next").json()
    assert "time_limit_ms" in q
    # New cards are MC — flat 5s window.
    assert q["time_limit_ms"] == 5000


def test_type_window_scales_with_length(loaded_client: TestClient) -> None:
    """Bump a card past the ease-promotion threshold and confirm the window grows."""
    import sqlite3

    from kana_quiz.db import db_path

    # Drive one specific card into type-mode by mutating its task_state.
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    word = conn.execute("SELECT * FROM words WHERE kana = ?", ("さかな",)).fetchone()
    word_id = word["id"]
    # Stamp both directions as introduced + promoted.
    conn.execute(
        "INSERT INTO task_state (word_id, task, ease, interval_days, "
        "repetitions, due_at, introduced_at) VALUES (?, ?, ?, ?, ?, datetime('now', '-1 day'), datetime('now'))",
        (word_id, "en2ja", 2.6, 1.0, 2),
    )
    conn.execute(
        "INSERT INTO task_state (word_id, task, ease, interval_days, "
        "repetitions, due_at, introduced_at) VALUES (?, ?, ?, ?, ?, datetime('now', '-1 day'), datetime('now'))",
        (word_id, "ja2en", 2.6, 1.0, 2),
    )
    # Ignore every other word so the picker is forced to surface this one.
    conn.execute(
        "UPDATE words SET ignored_at = datetime('now') WHERE id != ?", (word_id,)
    )
    conn.commit()
    conn.close()

    q = loaded_client.get("/api/session/next").json()
    assert q["word_id"] == word_id
    assert q["mode"] == "type"
    # The timer sizes to expected keystrokes, not glyph count:
    #   * en2ja (type Japanese): "さかな" → "sakana" = 6 romaji keystrokes
    #     → 5000 + 6*600 = 8600 ms.
    #   * ja2en (type English): gloss "fish" averages to 4 chars → 5000
    #     + 4*600 = 7400 ms → clamped to the 8000 ms floor.
    # Picker chooses direction randomly; allow both.
    assert q["time_limit_ms"] in (8000, 8600), q
