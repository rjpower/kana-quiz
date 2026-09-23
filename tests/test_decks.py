"""Deck editing and deletion: the level field and the delete path."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _deck(client: TestClient, deck_id: int) -> dict:
    decks = client.get("/api/decks").json()
    return next(d for d in decks if d["id"] == deck_id)


def _populated_deck(client: TestClient, sample_csv: bytes, name: str) -> int:
    resp = client.post(
        "/api/import",
        files={"file": ("vocab.csv", sample_csv, "text/csv")},
        data={"new_deck_name": name, "new_deck_level": "5"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["deck_id"]


def test_patch_level_alone_persists(client: TestClient) -> None:
    """A PATCH carrying only ``level`` changes the level and keeps the name."""
    created = client.post("/api/decks", json={"name": "Grammar", "level": 5})
    assert created.status_code == 201, created.text
    deck_id = created.json()["id"]

    resp = client.patch(f"/api/decks/{deck_id}", json={"level": 2})
    assert resp.status_code == 200, resp.text
    assert resp.json()["level"] == 2
    assert resp.json()["name"] == "Grammar"
    assert _deck(client, deck_id)["level"] == 2


def test_patch_level_reorders_the_list(client: TestClient) -> None:
    """The deck list is ordered by level, so an edit moves the deck."""
    first = client.post("/api/decks", json={"name": "Alpha", "level": 2}).json()
    second = client.post("/api/decks", json={"name": "Beta", "level": 3}).json()

    order = [d["id"] for d in client.get("/api/decks").json()]
    assert order.index(first["id"]) < order.index(second["id"])

    assert client.patch(f"/api/decks/{first['id']}", json={"level": 9}).status_code == 200

    order = [d["id"] for d in client.get("/api/decks").json()]
    assert order.index(second["id"]) < order.index(first["id"])


def test_patch_level_on_missing_deck_is_404(client: TestClient) -> None:
    assert client.patch("/api/decks/9999", json={"level": 4}).status_code == 404


def test_delete_empty_deck(client: TestClient) -> None:
    deck_id = client.post("/api/decks", json={"name": "Scratch", "level": 5}).json()["id"]
    assert client.delete(f"/api/decks/{deck_id}").status_code == 204
    assert all(d["id"] != deck_id for d in client.get("/api/decks").json())


def test_delete_populated_deck_needs_force(
    client: TestClient, sample_csv: bytes
) -> None:
    """Without ``force`` a deck holding words is refused, and nothing is lost."""
    deck_id = _populated_deck(client, sample_csv, "Episode 1")

    resp = client.delete(f"/api/decks/{deck_id}")
    assert resp.status_code == 409
    assert "10 words" in resp.json()["detail"]
    assert _deck(client, deck_id)["word_count"] == 10


def test_force_delete_removes_the_words_and_their_history(
    client: TestClient, sample_csv: bytes, db_path: Path
) -> None:
    """``force=true`` deletes the deck, its words, and the SRS rows behind them."""
    deck_id = _populated_deck(client, sample_csv, "Episode 1")
    conn = _conn(db_path)
    word_ids = [
        r["id"]
        for r in conn.execute("SELECT id FROM words WHERE deck_id = ?", (deck_id,))
    ]
    assert len(word_ids) == 10
    conn.execute(
        "INSERT OR REPLACE INTO task_state (word_id, task, due_at)"
        " VALUES (?, 'en2ja', '2026-01-01')",
        (word_ids[0],),
    )
    conn.commit()

    resp = client.delete(f"/api/decks/{deck_id}?force=true")
    assert resp.status_code == 204, resp.text

    assert all(d["id"] != deck_id for d in client.get("/api/decks").json())
    conn = _conn(db_path)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM words WHERE deck_id = ?", (deck_id,)
    ).fetchone()["n"] == 0
    placeholders = ",".join("?" * len(word_ids))
    assert conn.execute(
        f"SELECT COUNT(*) AS n FROM task_state WHERE word_id IN ({placeholders})",
        word_ids,
    ).fetchone()["n"] == 0


def test_force_delete_leaves_other_decks_alone(
    client: TestClient, sample_csv: bytes, db_path: Path
) -> None:
    keep_id = _populated_deck(client, sample_csv, "Keep")
    kept = _deck(client, keep_id)["word_count"]
    drop_id = client.post("/api/decks", json={"name": "Drop", "level": 5}).json()["id"]

    assert client.delete(f"/api/decks/{drop_id}?force=true").status_code == 204
    assert _deck(client, keep_id)["word_count"] == kept


def test_delete_missing_deck_is_404(client: TestClient) -> None:
    assert client.delete("/api/decks/9999?force=true").status_code == 404
