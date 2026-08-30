"""CSV upload endpoint."""

from fastapi.testclient import TestClient


def _default_deck_id(client: TestClient) -> int:
    decks = client.get("/api/decks").json()
    return next(d["id"] for d in decks if d["name"] == "Default")


def test_import_inserts_all_rows(client: TestClient, sample_csv: bytes) -> None:
    deck_id = _default_deck_id(client)
    resp = client.post(
        "/api/import",
        files={"file": ("vocab.csv", sample_csv, "text/csv")},
        data={"deck_id": str(deck_id)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"inserted": 10, "updated": 0, "skipped": 0, "deck_id": 1, "deck_name": "Default"}

    stats = client.get("/api/stats").json()
    assert stats["total_words"] == 10
    assert stats["introduced"] == 0


def test_reimport_updates_instead_of_duplicating(
    client: TestClient, sample_csv: bytes
) -> None:
    deck_id = _default_deck_id(client)
    client.post(
        "/api/import",
        files={"file": ("v.csv", sample_csv, "text/csv")},
        data={"deck_id": str(deck_id)},
    )
    updated_csv = sample_csv.replace(b"dog", b"doggo")
    resp = client.post(
        "/api/import",
        files={"file": ("v.csv", updated_csv, "text/csv")},
        data={"deck_id": str(deck_id)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"inserted": 0, "updated": 10, "skipped": 0, "deck_id": 1, "deck_name": "Default"}

    stats = client.get("/api/stats").json()
    assert stats["total_words"] == 10


def test_missing_required_columns_returns_400(client: TestClient) -> None:
    deck_id = _default_deck_id(client)
    payload = "kana,notes\nいぬ,woof\n".encode("utf-8")
    resp = client.post(
        "/api/import",
        files={"file": ("bad.csv", payload, "text/csv")},
        data={"deck_id": str(deck_id)},
    )
    assert resp.status_code == 400
    assert "english" in resp.json()["detail"]


def test_import_with_srs_state_columns(client: TestClient) -> None:
    deck_id = _default_deck_id(client)
    # Mimic an Anki-style export: pre-seed interval/ease/reps so mature cards
    # don't get re-learned from scratch. SRS state is mirrored to BOTH
    # recall task_state rows.
    payload = (
        "kana,english,interval_days,ease,repetitions\n"
        "いぬ,dog,14,2.6,4\n"
        "ねこ,cat,0.5,2.3,1\n"
        "うま,horse,,,\n"
    ).encode("utf-8")
    resp = client.post(
        "/api/import",
        files={"file": ("anki.csv", payload, "text/csv")},
        data={"deck_id": str(deck_id)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 3, "updated": 0, "skipped": 0, "deck_id": 1, "deck_name": "Default"}

    detailed = client.get("/api/stats/detailed").json()
    by_kana = {w["kana"]: w for w in detailed["words"]}
    inu_en2ja = by_kana["いぬ"]["directions"]["en2ja"]
    assert inu_en2ja["interval_days"] == 14.0
    assert inu_en2ja["repetitions"] == 4
    assert inu_en2ja["maturity"] == "mature"
    assert inu_en2ja["introduced_at"] is not None
    # ja2en seeded too — same payload applied to both directions.
    assert by_kana["いぬ"]["directions"]["ja2en"]["interval_days"] == 14.0
    # Plain row with no SRS columns stays in "new" bucket and has no
    # recall task_state rows yet.
    assert by_kana["うま"]["maturity"] == "new"
    assert by_kana["うま"]["directions"]["en2ja"]["introduced_at"] is None


def test_reimport_without_srs_columns_preserves_state(client: TestClient) -> None:
    deck_id = _default_deck_id(client)
    seed = (
        "kana,english,interval_days,ease,repetitions\n"
        "いぬ,dog,14,2.6,4\n"
    ).encode("utf-8")
    client.post(
        "/api/import",
        files={"file": ("a.csv", seed, "text/csv")},
        data={"deck_id": str(deck_id)},
    )

    # A follow-up upload without SRS columns should *not* trample the live
    # scheduler state just because the importer happens to rename an english
    # gloss.
    reimport = b"kana,english\n\xe3\x81\x84\xe3\x81\xac,doggo\n"
    client.post(
        "/api/import",
        files={"file": ("b.csv", reimport, "text/csv")},
        data={"deck_id": str(deck_id)},
    )

    detailed = client.get("/api/stats/detailed").json()
    w = next(x for x in detailed["words"] if x["kana"] == "いぬ")
    assert w["english"] == "doggo"
    assert w["directions"]["en2ja"]["interval_days"] == 14.0
    assert w["directions"]["en2ja"]["repetitions"] == 4


def test_rows_missing_values_are_skipped(client: TestClient) -> None:
    deck_id = _default_deck_id(client)
    payload = (
        "kana,english\n"
        "いぬ,dog\n"
        ",orphan\n"
        "ねこ,\n"
        "うま,horse\n"
    ).encode("utf-8")
    resp = client.post(
        "/api/import",
        files={"file": ("v.csv", payload, "text/csv")},
        data={"deck_id": str(deck_id)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 2, "updated": 0, "skipped": 2, "deck_id": 1, "deck_name": "Default"}
