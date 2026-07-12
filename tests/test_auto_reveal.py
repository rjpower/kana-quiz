"""New-card auto-reveal: /session/next ships a cached sentence on first sighting.

The teaching payload is strictly cache-only (gemini.get_cached_sentence) so the
hot path never blocks on generation. On a cache miss the field is ``None`` and
the client fetches on demand as before; ``reveal=0`` suppresses it entirely.
"""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from kana_quiz import gemini


def _cache_sentence_for_all(db_path: Path) -> None:
    """Cache the same teaching sentence for every word.

    New-word introduction samples randomly within a deck level, so the first
    word ``/next`` hands back isn't predictable — caching all of them means
    whichever one is introduced carries the sentence.
    """
    conn = sqlite3.connect(db_path)
    conn.executemany(
        """
        INSERT INTO sentence_cache
          (word_id, model, japanese, english, mnemonic, target_form, created_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        [
            (row[0], gemini.DEFAULT_MODEL, "犬が好き。", "I like dogs.", "いぬ sounds like 'e-noo'", "")
            for row in conn.execute("SELECT id FROM words").fetchall()
        ],
    )
    conn.commit()
    conn.close()


def test_auto_reveal_ships_cached_sentence(
    loaded_client: TestClient, db_path: Path
) -> None:
    _cache_sentence_for_all(db_path)

    q = loaded_client.get("/api/session/next").json()
    assert q.get("introduction") is not None
    assert q.get("sentence") is not None
    assert q["sentence"]["japanese"] == "犬が好き。"
    assert q["sentence"]["mnemonic"] == "いぬ sounds like 'e-noo'"


def test_auto_reveal_none_on_cache_miss(
    loaded_client: TestClient, db_path: Path
) -> None:
    # No sentence_cache row -> sentence is None (and nothing is generated).
    q = loaded_client.get("/api/session/next").json()
    assert q.get("introduction") is not None
    assert q.get("sentence") is None


def test_reveal_zero_suppresses_sentence(
    loaded_client: TestClient, db_path: Path
) -> None:
    _cache_sentence_for_all(db_path)

    q = loaded_client.get("/api/session/next?reveal=0").json()
    assert q.get("introduction") is not None
    assert q.get("sentence") is None
