"""GET /api/stats/overview — the landing-page rollup.

Exercises the due lookahead windows, the mastery distribution, and the
active-set (ignored-excluded) accounting. Reviews are driven through the real
answer endpoint so task_state / reviews rows are written the production way.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient


def _introduce(client: TestClient, n: int) -> list[int]:
    """Introduce n new words via the new pool and answer each, returning ids.

    Answering is what makes a word introduced — the picker deals cards ahead of
    the user into a prefetch buffer and leaves ``introduced_at`` NULL until one
    comes back answered, so merely fetching a card would leave these words in
    the "new" pool and count for nothing here.
    """
    seen: list[int] = []
    for _ in range(n):
        ex = "&exclude=" + ",".join(map(str, seen)) if seen else ""
        r = client.get(f"/api/session/next?pool=new{ex}")
        if r.status_code != 200:
            break
        q = r.json()
        client.post(
            "/api/session/answer",
            json={
                "word_id": q["word_id"],
                "direction": q["direction"],
                "chosen_index": q["correct_index"],
                "correct_index": q["correct_index"],
                "timed_out": False,
                "latency_ms": 1200,
            },
        )
        seen.append(q["word_id"])
    return seen


def test_overview_shape_and_active_totals(loaded_client: TestClient) -> None:
    ov = loaded_client.get("/api/stats/overview").json()
    # Sample deck has 10 words, none introduced yet.
    assert ov["total_words"] == 10
    assert ov["introduced"] == 0
    assert ov["new_available"] == 10
    assert ov["due"] == {"due_now": 0, "next_hour": 0, "next_24h": 0}
    # Every word sits in the "new" tier before introduction.
    tiers = {t["maturity"]: t for t in ov["maturity"]}
    assert list(tiers) == ["new", "learning", "young", "mature", "mastered"]
    assert tiers["new"]["count"] == 10
    assert tiers["new"]["avg_ease"] is None
    assert ov["reviews_last_7_days"] == 0
    assert ov["accuracy_last_7_days"] is None


def test_overview_new_available_drops_after_introduction(
    loaded_client: TestClient,
) -> None:
    _introduce(loaded_client, 3)
    ov = loaded_client.get("/api/stats/overview").json()
    assert ov["introduced"] == 3
    assert ov["new_available"] == 7
    # One correct answer takes a lane to repetitions=1 / interval>=1d, and the
    # rollup buckets a word by its *most* mature lane — so these land in young,
    # not learning, even though each word's other lane is still untouched.
    tiers = {t["maturity"]: t for t in ov["maturity"]}
    assert tiers["young"]["count"] == 3
    assert tiers["young"]["avg_ease"] is not None
    assert tiers["learning"]["count"] == 0
    assert tiers["new"]["count"] == 7


def test_overview_due_windows_bucket_by_time(
    loaded_client: TestClient, db_path: Path
) -> None:
    ids = _introduce(loaded_client, 3)
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(db_path)
    # Stamp three introduced lanes at now-ish, +30min, +12h so each lands in a
    # different lookahead window. Touch the en2ja lane which every word has.
    offsets = {ids[0]: -60, ids[1]: 30 * 60, ids[2]: 12 * 3600}
    for wid, secs in offsets.items():
        conn.execute(
            "UPDATE task_state SET due_at = ?, introduced_at = ? "
            "WHERE word_id = ? AND task = 'en2ja'",
            (
                (now + timedelta(seconds=secs)).isoformat(),
                now.isoformat(),
                wid,
            ),
        )
    # Push the other lanes far out so they don't pollute the windows.
    conn.execute(
        "UPDATE task_state SET due_at = ? WHERE task = 'ja2en'",
        ((now + timedelta(days=30)).isoformat(),),
    )
    conn.commit()
    conn.close()

    due = loaded_client.get("/api/stats/overview").json()["due"]
    assert due["due_now"] == 1  # the -60s lane
    assert due["next_hour"] == 1  # the +30min lane
    assert due["next_24h"] == 2  # +30min and +12h, both within 24h, excl. now


def test_overview_excludes_ignored_words(loaded_client: TestClient) -> None:
    base = loaded_client.get("/api/stats/overview").json()
    assert base["total_words"] == 10
    # Ignore one word; it should drop from total + new_available.
    wid = loaded_client.get("/api/session/next?pool=new").json()["word_id"]
    loaded_client.post(f"/api/words/{wid}/ignore").raise_for_status()
    after = loaded_client.get("/api/stats/overview").json()
    assert after["total_words"] == 9
    # The ignored word was introduced then ignored → not new, not counted active.
    assert after["new_available"] == 9
