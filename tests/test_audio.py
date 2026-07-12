"""Audio endpoint + TTS cache behavior.

These tests monkeypatch :func:`kana_quiz.tts.synthesize` so we never hit
the OpenAI API, and assert the sqlite-backed cache prevents re-synthesis
on repeat requests.
"""

import pytest
from fastapi.testclient import TestClient

from kana_quiz import tts


@pytest.fixture
def fake_synthesize(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the TTS call with a stub; return a list of requested texts."""
    calls: list[str] = []

    def _fake(text: str, *, voice: str, model: str) -> bytes:
        calls.append(text)
        return b"ID3fake-mp3-bytes-for-" + text.encode("utf-8")

    monkeypatch.setattr(tts, "synthesize", _fake)
    return calls


def test_audio_endpoint_caches_after_first_call(
    loaded_client: TestClient, fake_synthesize: list[str]
) -> None:
    word_id = loaded_client.get("/api/session/next").json()["word_id"]

    r1 = loaded_client.get(f"/api/audio/word/{word_id}")
    assert r1.status_code == 200
    assert r1.headers["content-type"] == "audio/mpeg"
    assert r1.content.startswith(b"ID3fake-mp3-bytes-for-")
    assert "etag" in {k.lower() for k in r1.headers}
    assert len(fake_synthesize) == 1

    r2 = loaded_client.get(f"/api/audio/word/{word_id}")
    assert r2.status_code == 200
    assert r2.content == r1.content
    # Cache hit: no additional synthesis call.
    assert len(fake_synthesize) == 1


def test_audio_endpoint_honors_if_none_match(
    loaded_client: TestClient, fake_synthesize: list[str]
) -> None:
    word_id = loaded_client.get("/api/session/next").json()["word_id"]
    r1 = loaded_client.get(f"/api/audio/word/{word_id}")
    etag = r1.headers["etag"]

    r2 = loaded_client.get(
        f"/api/audio/word/{word_id}", headers={"If-None-Match": etag}
    )
    assert r2.status_code == 304
    assert r2.content == b""


def test_audio_endpoint_returns_503_when_tts_unavailable(
    loaded_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credentials / TTS disabled -> 503 so the UI hides the speaker button."""
    def _unavailable(text: str, *, voice: str, model: str) -> bytes:
        raise tts.TTSUnavailable("no creds")

    monkeypatch.setattr(tts, "synthesize", _unavailable)
    word_id = loaded_client.get("/api/session/next").json()["word_id"]
    r = loaded_client.get(f"/api/audio/word/{word_id}")
    assert r.status_code == 503


def test_audio_endpoint_404_for_unknown_word(
    loaded_client: TestClient, fake_synthesize: list[str]
) -> None:
    r = loaded_client.get("/api/audio/word/999999")
    assert r.status_code == 404


def test_upcoming_returns_word_ids_for_prefetch(loaded_client: TestClient) -> None:
    r = loaded_client.get("/api/session/upcoming?n=5")
    assert r.status_code == 200
    body = r.json()
    assert "word_ids" in body
    ids = body["word_ids"]
    assert len(ids) == 5
    assert len(set(ids)) == 5  # no duplicates
    assert all(isinstance(i, int) for i in ids)


def test_upcoming_caps_at_deck_size(loaded_client: TestClient) -> None:
    # Sample CSV has 10 words; asking for more should return at most 10.
    r = loaded_client.get("/api/session/upcoming?n=50")
    assert r.status_code == 200
    assert len(r.json()["word_ids"]) == 10


def test_upcoming_does_not_consume_words(loaded_client: TestClient) -> None:
    """Peek must not mark words introduced — that's pick_next_word's job."""
    before = loaded_client.get("/api/session/upcoming?n=10").json()["word_ids"]
    after = loaded_client.get("/api/session/upcoming?n=10").json()["word_ids"]
    assert before == after
