"""Sentence cards, deck-scoped sessions, and sprint archiving."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

SENTENCE_CSV = "\n".join(
    [
        "kana,english,kind,tags",
        "今日はとても暑いですね。,It is very hot today.,sentence,ep1",
        "昨日は海に行きました。,I went to the sea yesterday.,sentence,ep1",
        "この番組を聞いてくれてありがとう。,Thank you for listening to this show.,sentence,ep1",
    ]
).encode()


def _import_sprint_deck(client: TestClient, name: str = "Episode 1") -> int:
    resp = client.post(
        "/api/import",
        files={"file": ("ep1.csv", SENTENCE_CSV, "text/csv")},
        data={
            "new_deck_name": name,
            "new_deck_level": "5",
            "new_deck_profile": "sprint",
            "insert_only": "true",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["inserted"] == 3
    assert body["deck_name"] == name
    return body["deck_id"]


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_sentence_import_creates_sprint_deck(client: TestClient, db_path: Path):
    deck_id = _import_sprint_deck(client)
    decks = {d["id"]: d for d in client.get("/api/decks").json()}
    deck = decks[deck_id]
    assert deck["profile"] == "sprint"
    assert deck["word_count"] == 3
    conn = _conn(db_path)
    kinds = {
        r["kind"]
        for r in conn.execute("SELECT kind FROM words WHERE deck_id = ?", (deck_id,))
    }
    conn.close()
    assert kinds == {"sentence"}


def test_insert_only_never_updates(client: TestClient, db_path: Path):
    deck_id = _import_sprint_deck(client)
    changed = SENTENCE_CSV.replace(b"It is very hot today.", b"CLOBBERED")
    resp = client.post(
        "/api/import",
        files={"file": ("ep1.csv", changed, "text/csv")},
        data={"deck_id": str(deck_id), "insert_only": "true"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "inserted": 0,
        "updated": 0,
        "skipped": 3,
        "deck_id": deck_id,
        "deck_name": "Episode 1",
    }
    conn = _conn(db_path)
    english = conn.execute(
        "SELECT english FROM words WHERE kana = ?", ("今日はとても暑いですね。",)
    ).fetchone()["english"]
    conn.close()
    assert english == "It is very hot today."


def test_deck_scoped_batch_serves_only_that_deck(
    client: TestClient, db_path: Path, sample_csv: bytes
):
    resp = client.post(
        "/api/import",
        files={"file": ("words.csv", sample_csv, "text/csv")},
        data={"new_deck_name": "Words", "new_deck_level": "1"},
    )
    assert resp.status_code == 200
    deck_id = _import_sprint_deck(client)

    batch = client.get(f"/api/session/batch?n=12&deck={deck_id}").json()["questions"]
    assert batch, "deck session should introduce the deck's sentences"
    conn = _conn(db_path)
    deck_word_ids = {
        r["id"]
        for r in conn.execute("SELECT id FROM words WHERE deck_id = ?", (deck_id,))
    }
    conn.close()
    for q in batch:
        assert q["word_id"] in deck_word_ids
        assert q["kind"] == "sentence"
        assert q["mode"] == "type"
        assert q["direction"] == "ja2en"
        assert q["choices"] == []


def test_sentence_card_has_single_lane(client: TestClient, db_path: Path):
    deck_id = _import_sprint_deck(client)
    q = client.get(f"/api/session/next?deck={deck_id}").json()
    client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "timed_out": False,
            "typed_answer": "It is very hot today.",
            "mode": "type",
        },
    )
    conn = _conn(db_path)
    tasks = [
        r["task"]
        for r in conn.execute(
            "SELECT task FROM task_state WHERE word_id = ?", (q["word_id"],)
        )
    ]
    conn.close()
    assert tasks == ["ja2en"]


def test_sprint_card_archives_on_second_spaced_success(
    client: TestClient, db_path: Path
):
    deck_id = _import_sprint_deck(client)
    q = client.get(f"/api/session/next?deck={deck_id}").json()
    word_id = q["word_id"]

    def answer() -> dict:
        resp = client.post(
            "/api/session/answer",
            json={
                "word_id": word_id,
                "direction": "ja2en",
                "timed_out": False,
                "typed_answer": q["prompt"],
                "mode": "type",
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    # No grader is configured under test, so type the reference translation.
    conn = _conn(db_path)
    reference = conn.execute(
        "SELECT english FROM words WHERE id = ?", (word_id,)
    ).fetchone()["english"]
    conn.close()
    q["prompt"] = reference

    first = answer()
    assert first["correct"] and not first["archived"]
    second = answer()
    assert second["correct"] and not second["archived"]
    third = answer()
    assert third["correct"] and third["archived"]

    conn = _conn(db_path)
    row = conn.execute(
        "SELECT archived_at, ignored_at FROM words WHERE id = ?", (word_id,)
    ).fetchone()
    conn.close()
    assert row["archived_at"] is not None
    assert row["ignored_at"] is not None

    served = client.get(f"/api/session/batch?n=12&deck={deck_id}").json()["questions"]
    assert word_id not in {s["word_id"] for s in served}


def test_standard_deck_never_archives(client: TestClient, db_path: Path, sample_csv):
    resp = client.post(
        "/api/import",
        files={"file": ("words.csv", sample_csv, "text/csv")},
        data={"new_deck_name": "Words", "new_deck_level": "1"},
    )
    deck_id = resp.json()["deck_id"]
    q = client.get(f"/api/session/next?deck={deck_id}").json()
    for _ in range(4):
        resp = client.post(
            "/api/session/answer",
            json={
                "word_id": q["word_id"],
                "direction": q["direction"],
                "chosen_index": q.get("correct_index", 0),
                "correct_index": q.get("correct_index", 0),
                "timed_out": False,
                "mode": q["mode"],
            },
        )
        body = resp.json()
        assert body["archived"] is False
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT archived_at FROM words WHERE id = ?", (q["word_id"],)
    ).fetchone()
    conn.close()
    assert row["archived_at"] is None


def test_bearer_token_opens_the_gate(
    db_path: Path, monkeypatch, sample_csv: bytes
):
    import importlib

    monkeypatch.setenv("KANA_AUTH_PASSWORD", "hunter2")
    monkeypatch.setenv("KANA_API_TOKEN", "deck-courier")
    from kana_quiz import main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as anon:
        locked = anon.get("/api/decks")
        assert locked.status_code == 401
        wrong = anon.get(
            "/api/decks", headers={"Authorization": "Bearer not-the-token"}
        )
        assert wrong.status_code == 401
        opened = anon.get(
            "/api/decks", headers={"Authorization": "Bearer deck-courier"}
        )
        assert opened.status_code == 200
