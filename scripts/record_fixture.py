"""Record a scripted study session as a replay fixture.

The app runs in this process on a copy of the live database with the
recorder on (see ``kana_quiz.pins``). The model is a fake whose answers
are deterministic text, so every call is recorded and later replayed
against each backend. The output is one JSON line per request.

    uv run python scripts/record_fixture.py --out fixtures/session.jsonl

The source database is copied and never written. The copy the session
ran on is discarded; the untouched copy is kept as <out>.sqlite, since
the fixture replays only against that state.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import random
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "kana_quiz.sqlite"


class FakeModel:
    """Answers every prompt with plausible JSON for the call that made it."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_content(self, *, model: str, contents: str, config: object) -> object:
        self.calls += 1
        n = self.calls
        if "flashcard for an English-speaking" in contents:
            text = json.dumps({"kana": f"ことば{n}", "kanji": f"言葉{n}", "english": f"word {n}"})
        elif "grading a Japanese-vocabulary student" in contents:
            verdict = ["correct", "accept", "incorrect"][n % 3]
            text = json.dumps(
                {
                    "verdict": verdict,
                    "explanation": f"scripted verdict {n}",
                    "alternates": ["alt one", "alt two"] if verdict == "correct" else [],
                    "clarified_gloss": "",
                }
            )
        else:
            text = json.dumps(
                {
                    "japanese": f"これは例文{n}です。",
                    "english": f"This is example sentence {n}.",
                    "mnemonic": f"hook {n}",
                    "target_form": "例文",
                }
            )

        class Response:
            pass

        r = Response()
        r.text = text  # type: ignore[attr-defined]
        return r


class FakeClient:
    def __init__(self) -> None:
        self.models = FakeModel()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261023)
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        args.out.unlink()
    # The fixture is only valid against the database it was recorded on, so
    # the untouched copy is kept beside it as <out>.sqlite.
    snapshot = args.out.with_suffix(".sqlite")
    shutil.copyfile(args.db, snapshot)
    work = Path(tempfile.mkdtemp(prefix="kana-record-"))
    db_copy = work / "kana_quiz.sqlite"
    shutil.copyfile(snapshot, db_copy)

    os.environ["KANA_QUIZ_DB"] = str(db_copy)
    os.environ["KANA_RECORD"] = str(args.out)
    os.environ["KANA_TTS_DISABLED"] = "1"
    os.environ["KANA_PREFETCH_DISABLED"] = "1"
    os.environ["GEMINI_API_KEY"] = "recorded"
    os.environ.pop("KANA_AUTH_PASSWORD", None)
    os.environ.pop("KANA_API_TOKEN", None)

    from fastapi.testclient import TestClient

    from kana_quiz import gemini
    from kana_quiz import main as main_module

    importlib.reload(main_module)
    gemini._get_client = lambda: FakeClient()  # type: ignore[assignment]

    rng = random.Random(args.seed)
    with TestClient(main_module.app) as c:
        play(c, rng)
    shutil.rmtree(work, ignore_errors=True)
    print(f"wrote {sum(1 for _ in args.out.open())} requests to {args.out}")


def play(c, rng: random.Random) -> None:  # noqa: ANN001
    """One study session that touches every route."""
    def ok(resp, *codes: int):  # noqa: ANN001
        assert resp.status_code in (codes or (200,)), f"{resp.request.url} -> {resp.status_code} {resp.text[:200]}"
        return resp

    ok(c.get("/api/auth/status"))
    decks = ok(c.get("/api/decks")).json()
    ok(c.get("/api/stats"))
    ok(c.get("/api/stats/overview", params={"tz_offset": 420}))
    ok(c.get("/api/stats/detailed"))
    ok(c.get("/api/stats/debug"))
    ok(c.get("/api/glosses"))
    ok(c.get("/api/gemini_log", params={"limit": 20}))
    ok(c.post("/api/client_log", json={"url": "/api/session/batch", "outcome": "slow", "duration_ms": 2400, "attempts": 2}), 200, 204)

    for deck in decks[:4]:
        ok(c.get(f"/api/decks/{deck['id']}/words"))
        ok(c.get(f"/api/decks/{deck['id']}/ignored"))

    modes_all = "mc,type,cloze,cloze_choice,sentence_listen"

    def answer_for(q: dict, outcome: str) -> dict:
        body = {
            "word_id": q["word_id"],
            "direction": q["direction"],
            "mode": q["mode"],
            "timed_out": False,
            "latency_ms": rng.randint(400, 4000),
            "cloze_expected": q.get("cloze_expected"),
        }
        if q["mode"] in ("mc", "cloze_choice"):
            body["correct_index"] = q["correct_index"]
            if outcome == "timeout":
                body["timed_out"] = True
                body["chosen_index"] = None
            elif outcome == "wrong":
                body["chosen_index"] = (q["correct_index"] + 1) % max(1, len(q["choices"]))
            else:
                body["chosen_index"] = q["correct_index"]
        else:
            if outcome == "timeout":
                body["timed_out"] = True
                body["typed_answer"] = ""
            elif outcome == "wrong":
                body["typed_answer"] = "まちがい"
            elif outcome == "gaveup":
                body["gave_up"] = True
                body["typed_answer"] = ""
            else:
                body["typed_answer"] = q.get("expected") or q.get("cloze_expected") or q["prompt"]
        return body

    outcomes = ["right", "right", "right", "wrong", "timeout", "gaveup"]

    # Three rounds: mixed modes, new words only, one deck.
    rounds = [
        {"modes": modes_all, "reveal": "1", "pool": "all"},
        {"modes": "mc,type", "reveal": "0", "pool": "new"},
        {"modes": "mc", "reveal": "1", "pool": "all", "deck": str(decks[0]["id"])},
    ]
    for query in rounds:
        held: list[int] = []
        for _ in range(3):
            params = {**query, "n": "8"}
            if held:
                params["exclude"] = ",".join(str(i) for i in held[-8:])
            batch = ok(c.get("/api/session/batch", params=params), 200, 204)
            if batch.status_code == 204:
                break
            questions = batch.json()["questions"]
            if not questions:
                break
            ok(c.get("/api/session/upcoming", params={"n": "12"}))
            for q in questions:
                held.append(q["word_id"])
                ok(c.post("/api/session/answer", json=answer_for(q, rng.choice(outcomes))))
            single = ok(c.get("/api/session/next", params={**query, "exclude": str(held[-1])}), 200, 204)
            if single.status_code == 200:
                q = single.json()
                held.append(q["word_id"])
                ok(c.post("/api/session/answer", json=answer_for(q, "right")))

    # The drill after a round: each missed word once per direction.
    missed = held[:6]
    for word_id in missed:
        for direction in ("en2ja", "ja2en"):
            resp = ok(
                c.get(
                    "/api/session/drill",
                    params={"word_id": word_id, "direction": direction, "pool_ids": ",".join(map(str, missed))},
                ),
                200,
                404,
            )
            if resp.status_code == 200:
                ok(c.post("/api/session/answer", json=answer_for(resp.json(), rng.choice(outcomes[:4]))))

    # Match: tiles, then one answer per tile.
    seen: list[int] = []
    for _ in range(3):
        tiles = ok(c.get("/api/session/match-batch", params={"n": "6", "exclude_ids": ",".join(map(str, seen))})).json()["tiles"]
        for t in tiles:
            seen.append(t["word_id"])
            correct = rng.random() < 0.8
            ok(
                c.post(
                    "/api/session/answer",
                    json={
                        "word_id": t["word_id"],
                        "direction": t["direction"],
                        "mode": "mc",
                        "chosen_index": 0 if correct else 1,
                        "correct_index": 0,
                        "timed_out": False,
                        "latency_ms": rng.randint(300, 2500),
                    },
                )
            )

    # Sentences and audio for a few words.
    for word_id in held[:5]:
        ok(c.get(f"/api/words/{word_id}/sentence"), 200, 503)
        ok(c.get(f"/api/audio/word/{word_id}"), 200, 404, 503)
        ok(c.get(f"/api/audio/sentence/{word_id}"), 200, 404, 503)

    # Ignore and restore.
    for word_id in held[:3]:
        ok(c.post(f"/api/words/{word_id}/ignore"), 204)
    ok(c.get(f"/api/decks/{decks[0]['id']}/ignored"))
    ok(c.delete(f"/api/words/{held[0]}/ignore"), 204)

    # Quick add, twice the same term, then its sentence, which the model writes.
    added = ok(c.post("/api/words/quick", json={"text": "chair"})).json()
    ok(c.post("/api/words/quick", json={"text": "chair"}))
    ok(c.get(f"/api/words/{added['id']}/sentence"))
    ok(c.get(f"/api/words/{added['id']}/sentence"))
    ok(c.post("/api/words/quick", json={"text": ""}), 400, 422)

    # Decks: create, rename, relevel, reorder, import into it, delete.
    created = ok(c.post("/api/decks", json={"name": "Fixture deck", "level": 3}), 201).json()
    ok(c.post("/api/decks", json={"name": "Fixture deck", "level": 3}), 400, 409)
    ok(c.patch(f"/api/decks/{created['id']}", json={"name": "Fixture deck two"}))
    ok(c.patch(f"/api/decks/{created['id']}", json={"level": 9}))
    ok(c.patch(f"/api/decks/{created['id']}", json={"pick_order": "listed"}))
    ok(c.patch(f"/api/decks/{created['id']}", json={"profile": "sprint"}))
    ok(c.patch(f"/api/decks/{created['id']}", json={"level": -1}), 200, 400, 422)
    csv = "kana,english,kanji,tags\nきつね,fox,狐,animal\nたぬき,raccoon dog,狸,animal\nきつね,fox,狐,animal\n"
    ok(
        c.post(
            "/api/import",
            files={"file": ("fixture.csv", csv.encode("utf-8"), "text/csv")},
            data={"deck_id": str(created["id"])},
        )
    )
    ok(
        c.post(
            "/api/import",
            files={"file": ("fixture2.csv", csv.encode("utf-8"), "text/csv")},
            data={"new_deck_name": "Imported deck", "new_deck_level": "4"},
        )
    )
    ok(c.get(f"/api/decks/{created['id']}/words"))
    ok(c.delete(f"/api/decks/{created['id']}"), 400, 409)
    ok(c.delete(f"/api/decks/{created['id']}", params={"force": "true"}), 204)
    ok(c.get("/api/decks/999999/words"), 404)
    ok(c.delete("/api/decks/999999"), 404)

    ok(c.get("/api/stats"))
    ok(c.get("/api/stats/overview", params={"tz_offset": -540}))
    ok(c.get("/api/stats/detailed"))
    ok(c.get("/api/gemini_log", params={"limit": 50}))


if __name__ == "__main__":
    main()
