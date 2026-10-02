"""A new word is asked in both directions during its first sitting."""

from fastapi.testclient import TestClient


def _answer(client: TestClient, q: dict) -> None:
    resp = client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "chosen_index": q["correct_index"],
            "correct_index": q["correct_index"],
            "timed_out": False,
            "latency_ms": 1000,
        },
    )
    assert resp.status_code == 200, resp.text


def test_sibling_direction_follows_the_debut(loaded_client: TestClient) -> None:
    first = loaded_client.get("/api/session/next?pool=new").json()
    assert first["direction"] == "ja2en"
    _answer(loaded_client, first)

    second = loaded_client.get("/api/session/next?pool=new").json()
    assert second["word_id"] == first["word_id"]
    assert second["direction"] == "en2ja"
    assert second.get("introduction") is None

    # Once both directions are answered the pair is done; the next card is a
    # different word.
    _answer(loaded_client, second)
    third = loaded_client.get("/api/session/next?pool=new").json()
    assert third["word_id"] != first["word_id"]


def test_sibling_is_served_in_a_review_session_too(loaded_client: TestClient) -> None:
    first = loaded_client.get("/api/session/next?pool=new").json()
    _answer(loaded_client, first)
    second = loaded_client.get("/api/session/next?pool=review").json()
    assert (second["word_id"], second["direction"]) == (first["word_id"], "en2ja")


def test_sibling_respects_the_exclude_list(loaded_client: TestClient) -> None:
    first = loaded_client.get("/api/session/next?pool=new").json()
    _answer(loaded_client, first)
    other = loaded_client.get(f"/api/session/next?pool=new&exclude={first['word_id']}").json()
    assert other["word_id"] != first["word_id"]
