"""`pool` param on /session/next + /session/batch: review-only vs new-only."""

from fastapi.testclient import TestClient


def test_review_pool_204_when_nothing_due(loaded_client: TestClient) -> None:
    # A freshly-imported deck is all unintroduced — a review session has nothing
    # to serve and must NOT introduce a new word to fill the gap.
    r = loaded_client.get("/api/session/next?pool=review")
    assert r.status_code == 204


def test_new_pool_introduces_new_words(loaded_client: TestClient) -> None:
    q = loaded_client.get("/api/session/next?pool=new").json()
    # A brand-new card carries the introduction payload.
    assert q["introduction"] is not None


def test_new_pool_batch_is_distinct_and_all_new(loaded_client: TestClient) -> None:
    b = loaded_client.get("/api/session/batch?pool=new&n=5").json()
    qs = b["questions"]
    assert len(qs) == 5
    assert len({q["word_id"] for q in qs}) == 5  # no dupes
    assert all(q["introduction"] is not None for q in qs)  # every one a fresh word


def test_review_pool_serves_introduced_due_without_intro(loaded_client: TestClient) -> None:
    # Introduce a word via the new pool (stamps introduced_at + due now)...
    q = loaded_client.get("/api/session/next?pool=new").json()
    wid = q["word_id"]
    # ...then the review pool serves that now-due card and introduces nothing.
    r = loaded_client.get("/api/session/next?pool=review").json()
    assert r["introduction"] is None
    assert r["word_id"] == wid


def test_new_pool_runs_dry_returns_204(loaded_client: TestClient) -> None:
    # Introduce every word, then the new pool has nothing left to hand out.
    seen: set[int] = set()
    for _ in range(200):
        r = loaded_client.get("/api/session/next?pool=new")
        if r.status_code == 204:
            break
        wid = r.json()["word_id"]
        if wid in seen:
            break
        seen.add(wid)
        # exclude it so the next call moves on to a different fresh word
        r2 = loaded_client.get(f"/api/session/next?pool=new&exclude={wid}")
        if r2.status_code == 204:
            break
    # The sample deck is small; once drained, new pool 204s.
    drained = loaded_client.get(
        "/api/session/batch?pool=new&n=5&exclude=" + ",".join(map(str, seen))
    ).json()
    assert drained["questions"] == []


def test_default_pool_still_mixes(loaded_client: TestClient) -> None:
    # No pool param -> mixed: a fresh deck introduces new words as before.
    q = loaded_client.get("/api/session/next").json()
    assert q["introduction"] is not None
