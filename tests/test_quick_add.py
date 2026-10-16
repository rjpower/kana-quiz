"""POST /api/words/quick: one typed term becomes a card in the Quick deck."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kana_quiz import gemini


@pytest.fixture
def fake_resolve(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[str, ...]]]:
    """Replace the model with a table; return the (term, taken) pairs it saw."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    table = {
        "dog": gemini.Entry(kana="いぬ", kanji="犬", english="dog"),
        "犬": gemini.Entry(kana="いぬ", kanji="犬", english="dog"),
        "order": gemini.Entry(kana="めいれい", kanji="命令", english="order"),
        "bird": gemini.Entry(kana="とり", kanji="鳥", english="bird"),
    }

    def _fake(term: str, *, taken: tuple[str, ...] = (), model: str = gemini.DEFAULT_MODEL):
        calls.append((term, taken))
        entry = table[term]
        if taken:
            return gemini.Entry(kana=entry.kana, kanji=entry.kanji, english=f"{entry.english} (a command)")
        return entry

    monkeypatch.setattr(gemini, "resolve_term", _fake)
    monkeypatch.setattr(gemini, "schedule_prefetch", lambda: None)
    return calls


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_quick_add_files_the_word_in_the_quick_deck(client: TestClient, fake_resolve, db_path: Path) -> None:
    resp = client.post("/api/words/quick", json={"text": " dog "})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    assert (body["kana"], body["kanji"], body["english"]) == ("いぬ", "犬", "dog")
    assert body["deck_name"] == "Quick"
    row = _conn(db_path).execute("SELECT deck_id FROM words WHERE kana = 'いぬ'").fetchone()
    assert row["deck_id"] == body["deck_id"]
    assert fake_resolve == [("dog", ())]


def test_quick_add_answers_a_card_that_exists(client: TestClient, fake_resolve, sample_csv: bytes) -> None:
    imported = client.post(
        "/api/import",
        files={"file": ("vocab.csv", sample_csv, "text/csv")},
        data={"new_deck_name": "Animals", "new_deck_level": "5"},
    )
    assert imported.status_code == 200, imported.text
    by_kanji = client.post("/api/words/quick", json={"text": "犬"})
    assert by_kanji.status_code == 200, by_kanji.text
    assert by_kanji.json()["created"] is False
    assert by_kanji.json()["deck_name"] == "Animals"
    # A kanji the table holds is answered without a model call.
    assert fake_resolve == []
    by_english = client.post("/api/words/quick", json={"text": "bird"})
    assert by_english.json()["created"] is False
    assert fake_resolve == [("bird", ())]


def test_quick_add_disambiguates_a_taken_gloss(client: TestClient, fake_resolve) -> None:
    created = client.post("/api/decks", json={"name": "Grammar", "level": 5})
    deck_id = created.json()["id"]
    first = client.post(
        "/api/import",
        files={"file": ("vocab.csv", "kana,english\nちゅうもん,order\n".encode(), "text/csv")},
        data={"deck_id": str(deck_id)},
    )
    assert first.status_code == 200, first.text
    resp = client.post("/api/words/quick", json={"text": "order"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["english"] == "order (a command)"
    assert fake_resolve == [("order", ()), ("order", ("order",))]


def test_quick_add_without_gemini_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(term: str, *, taken=(), model=gemini.DEFAULT_MODEL):
        raise gemini.GeminiUnavailable("GEMINI_API_KEY not configured")

    monkeypatch.setattr(gemini, "resolve_term", boom)
    resp = client.post("/api/words/quick", json={"text": "dog"})
    assert resp.status_code == 503


def test_blank_text_is_400(client: TestClient) -> None:
    assert client.post("/api/words/quick", json={"text": "  "}).status_code == 400


def test_migration_renames_takoboto(client: TestClient, db_path: Path) -> None:
    """Migration 019 renames the Takoboto deck to Quick on an existing database."""
    import importlib.util

    conn = _conn(db_path)
    conn.execute("INSERT INTO decks (name, level, profile) VALUES ('Takoboto', 5, 'standard')")
    conn.commit()
    path = Path(gemini.__file__).parent / "migrations" / "019_quick_deck.py"
    spec = importlib.util.spec_from_file_location("m019", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.up(conn)
    conn.commit()
    names = {r["name"] for r in conn.execute("SELECT name FROM decks")}
    assert "Quick" in names and "Takoboto" not in names
