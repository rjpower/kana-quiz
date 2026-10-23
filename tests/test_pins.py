"""A request can pin its clock, its seed and its model answers, and a
recording run writes each request with its pins and its response."""

import importlib
import json
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from kana_quiz import gemini


def _reloaded_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> Iterator[TestClient]:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from kana_quiz import main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture
def pinned_client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _reloaded_client(monkeypatch, KANA_PINS="1")


@pytest.fixture
def recording_client(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _reloaded_client(monkeypatch, KANA_RECORD=str(tmp_path / "trace.jsonl"))


def _import(client: TestClient, sample_csv: bytes) -> None:
    decks = client.get("/api/decks").json()
    default_id = next(d["id"] for d in decks if d["name"] == "Default")
    resp = client.post(
        "/api/import",
        files={"file": ("vocab.csv", sample_csv, "text/csv")},
        data={"deck_id": str(default_id)},
    )
    assert resp.status_code == 200, resp.text


def test_the_clock_pin_sets_review_time(pinned_client: TestClient, sample_csv: bytes) -> None:
    _import(pinned_client, sample_csv)
    q = pinned_client.get("/api/session/next", params={"modes": "mc", "pool": "all"}).json()
    headers = {"X-Kana-Now": "2030-01-02T03:04:05+00:00", "X-Kana-Seed": "7"}
    resp = pinned_client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "mode": "mc",
            "chosen_index": q["correct_index"],
            "correct_index": q["correct_index"],
            "timed_out": False,
            "latency_ms": 900,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    detailed = pinned_client.get("/api/stats/detailed").json()
    row = next(w for w in detailed["words"] if w["id"] == q["word_id"])
    assert row["directions"][q["direction"]]["introduced_at"].startswith("2030-01-02T03:04:05")


def test_the_seed_pin_makes_the_question_repeat(pinned_client: TestClient, sample_csv: bytes) -> None:
    _import(pinned_client, sample_csv)
    params = {"modes": "mc", "pool": "all", "n": "6"}
    headers = {"X-Kana-Seed": "42"}
    first = pinned_client.get("/api/session/batch", params=params, headers=headers).json()
    second = pinned_client.get("/api/session/batch", params=params, headers=headers).json()
    assert first == second
    other = pinned_client.get("/api/session/batch", params=params, headers={"X-Kana-Seed": "43"}).json()
    choices = lambda batch: [q["choices"] for q in batch["questions"]]
    assert choices(other) != choices(first)


def test_headers_are_ignored_without_the_switch(client: TestClient, sample_csv: bytes) -> None:
    _import(client, sample_csv)
    q = client.get("/api/session/next", params={"modes": "mc", "pool": "all"}).json()
    client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "mode": "mc",
            "chosen_index": q["correct_index"],
            "correct_index": q["correct_index"],
            "timed_out": False,
            "latency_ms": 900,
        },
        headers={"X-Kana-Now": "2030-01-02T03:04:05+00:00"},
    )
    detailed = client.get("/api/stats/detailed").json()
    row = next(w for w in detailed["words"] if w["id"] == q["word_id"])
    assert not row["directions"][q["direction"]]["introduced_at"].startswith("2030")


def test_a_scripted_model_answer_is_used(pinned_client: TestClient, sample_csv: bytes) -> None:
    script = [{"name": "term", "text": json.dumps({"kana": "いす", "kanji": "椅子", "english": "chair"})}]
    resp = pinned_client.post(
        "/api/words/quick", json={"text": "chair"}, headers={"X-Kana-Model": json.dumps(script)}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kana"] == "いす"


def test_a_scripted_refusal_is_503(pinned_client: TestClient) -> None:
    script = [{"name": "term", "error": "quota"}]
    resp = pinned_client.post(
        "/api/words/quick", json={"text": "chair"}, headers={"X-Kana-Model": json.dumps(script)}
    )
    assert resp.status_code == 503
    assert "quota" in resp.json()["detail"]


def test_the_recorder_writes_pins_and_model_calls(
    recording_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Models:
        def generate_content(self, **_: object) -> object:
            class R:
                text = json.dumps({"kana": "いす", "kanji": "椅子", "english": "chair"})

            return R()

    class Client:
        models = Models()

    monkeypatch.setattr(gemini, "_get_client", lambda: Client())
    resp = recording_client.post("/api/words/quick", json={"text": "chair"})
    assert resp.status_code == 200, resp.text
    lines = (tmp_path / "trace.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["method"] == "POST" and entry["target"] == "/api/words/quick"
    assert entry["now"] and isinstance(entry["seed"], int)
    assert json.loads(entry["body"]["text"]) == {"text": "chair"}
    assert entry["model"][0]["name"] == "term"
    assert json.loads(entry["model"][0]["text"])["kana"] == "いす"
    assert entry["status"] == 200
    assert json.loads(entry["response"]["text"])["kana"] == "いす"
