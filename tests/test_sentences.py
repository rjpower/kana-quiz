"""Sentence endpoint + LLM cache behavior.

We monkeypatch :func:`kana_quiz.gemini.generate_sentence` so the tests
never hit the Gemini API. The cache assertion mirrors the audio test:
first call generates, second call serves from sqlite.
"""

import pytest
from fastapi.testclient import TestClient

from kana_quiz import gemini


@pytest.fixture
def fake_generate(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the Gemini call with a stub; return a list of word ids it saw."""
    calls: list[int] = []

    def _fake(word, *, model: str = gemini.DEFAULT_MODEL) -> gemini.Sentence:
        calls.append(word.id)
        japanese = f"これは{word.kana}です。"
        return gemini.Sentence(
            japanese=japanese,
            english=f"This is {word.english}.",
            mnemonic=f"Picture a {word.english.upper()} saying '{word.kana}'.",
            # target_form must be a literal substring of japanese so the
            # cache row isn't treated as "pre-cloze, regenerate me" on
            # the next read.
            target_form=word.kana,
        )

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini, "generate_sentence", _fake)
    # Mute the background prefetch worker's wakeup signal so tests only
    # exercise the foreground `get_or_create` path. With it active, the
    # worker would race the test, generating sentences off-thread, and
    # the call-count assertions become non-deterministic.
    monkeypatch.setattr(gemini, "schedule_prefetch", lambda: None)
    return calls


def test_sentence_endpoint_caches_after_first_call(
    loaded_client: TestClient, fake_generate: list[int]
) -> None:
    word_id = loaded_client.get("/api/session/next").json()["word_id"]

    r1 = loaded_client.get(f"/api/words/{word_id}/sentence")
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["word_id"] == word_id
    assert body["japanese"]
    assert body["english"]
    assert body["mnemonic"]
    assert "Picture a" in body["mnemonic"]
    assert fake_generate == [word_id]

    r2 = loaded_client.get(f"/api/words/{word_id}/sentence")
    assert r2.status_code == 200
    assert r2.json() == body
    # Cache hit: no additional generation call.
    assert fake_generate == [word_id]


def test_sentence_endpoint_404_on_unknown_word(
    loaded_client: TestClient, fake_generate: list[int]
) -> None:
    resp = loaded_client.get("/api/words/99999/sentence")
    assert resp.status_code == 404
    assert fake_generate == []


def test_sentence_endpoint_503_when_unconfigured(
    loaded_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No GEMINI_API_KEY set, no monkeypatched generator — the route must
    # surface the GeminiUnavailable as a clean 503 rather than a 500.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    word_id = loaded_client.get("/api/session/next").json()["word_id"]
    resp = loaded_client.get(f"/api/words/{word_id}/sentence")
    assert resp.status_code == 503
