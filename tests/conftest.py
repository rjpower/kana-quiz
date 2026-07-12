"""Shared fixtures: a fresh sqlite DB + FastAPI TestClient per test."""

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_real_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-disable real TTS for every test.

    Dev/CI machines carry working Google Application Default Credentials, so
    without this the audio prefetch worker started in the app lifespan would
    synthesize sample-deck audio against the live Google TTS API on every test
    run. The kill switch makes the real ``synthesize`` raise ``TTSUnavailable``;
    tests that exercise the cache monkeypatch ``tts.synthesize`` directly and
    are unaffected.
    """
    monkeypatch.setenv("KANA_TTS_DISABLED", "1")


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app at an empty sqlite file in ``tmp_path`` for the test."""
    path = tmp_path / "kana_quiz.sqlite"
    monkeypatch.setenv("KANA_QUIZ_DB", str(path))
    return path


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    """Return a TestClient wired to the per-test sqlite file.

    The app module is reloaded so ``KANA_QUIZ_DB`` is read fresh for each test.
    """
    import importlib

    from kana_quiz import main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture
def sample_csv() -> bytes:
    rows = [
        ("kana", "english", "kanji", "tags"),
        ("いぬ", "dog", "犬", "animal"),
        ("ねこ", "cat", "猫", "animal"),
        ("うま", "horse", "馬", "animal"),
        ("とり", "bird", "鳥", "animal"),
        ("さかな", "fish", "魚", "animal"),
        ("みず", "water", "水", "nature"),
        ("ひ", "fire", "火", "nature"),
        ("つき", "moon", "月", "nature"),
        ("ほし", "star", "星", "nature"),
        ("やま", "mountain", "山", "nature"),
    ]
    return "\n".join(",".join(r) for r in rows).encode("utf-8")


@pytest.fixture
def loaded_client(client: TestClient, sample_csv: bytes) -> TestClient:
    """Client with the sample CSV already imported into the Default deck."""
    decks = client.get("/api/decks").json()
    default_id = next(d["id"] for d in decks if d["name"] == "Default")
    resp = client.post(
        "/api/import",
        files={"file": ("vocab.csv", sample_csv, "text/csv")},
        data={"deck_id": str(default_id)},
    )
    assert resp.status_code == 200, resp.text
    return client


