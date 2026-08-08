"""GET /api/stats + /api/stats/detailed — progress summary and drill-down."""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from kana_quiz import gemini, tts
from kana_quiz.db import db_path, get_conn
from kana_quiz.schemas import (
    DetailedStats,
    DirectionState,
    DueWindows,
    LatencyBucket,
    MaturityTier,
    Stats,
    StatsOverview,
    WordState,
)
from kana_quiz.session import all_consecutive_failures
from kana_quiz.srs import (
    LEECH_FAILURE_THRESHOLD,
    MASTERED_INTERVAL_DAYS,
    MATURITY_ORDER,
    classify_maturity,
)
from kana_quiz.task_state import CARD_TASKS, TASK_EN2JA, TASK_JA2EN

# Buckets chosen to span the usual 5 s question window. The final bucket is
# open-ended so rare slow answers still show up.
LATENCY_BUCKETS_MS: tuple[tuple[int, int | None], ...] = (
    (0, 500),
    (500, 1000),
    (1000, 1500),
    (1500, 2000),
    (2000, 2500),
    (2500, 3000),
    (3000, 3500),
    (3500, 4000),
    (4000, 4500),
    (4500, 5000),
    (5000, None),
)



router = APIRouter()

_client_logger = logging.getLogger("kana_quiz.api")


class ClientLogIn(BaseModel):
    """A fetch anomaly reported by the SPA's retry wrapper."""

    url: str
    outcome: str  # "slow" | "retry" | "timeout" | "failed"
    duration_ms: int
    attempts: int = 1
    detail: str | None = None


@router.post("/client_log")
def client_log(body: ClientLogIn) -> dict:
    """Funnel client-perceived fetch stalls into the server log stream.

    The backend's own request timing can't see a hang that happens between
    the browser and the app — a network blip, a stalled connection, the
    frontend's 6s timeout-and-retry. The SPA reports those here so they land
    next to the request logs and become greppable. Best-effort; never trusted
    for anything but observability.
    """
    level = logging.WARNING if body.outcome in ("timeout", "failed") else logging.INFO
    _client_logger.log(
        level,
        "client %s %s in %dms (attempts=%d)%s",
        body.outcome,
        body.url,
        body.duration_ms,
        body.attempts,
        f" {body.detail}" if body.detail else "",
    )
    return {"ok": True}


def _summary(conn: sqlite3.Connection) -> Stats:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    seven_days_ago_iso = (now - timedelta(days=7)).isoformat()

    total = conn.execute("SELECT COUNT(*) AS n FROM words").fetchone()["n"]
    introduced = conn.execute(
        """
        SELECT COUNT(DISTINCT word_id) AS n FROM task_state
         WHERE task IN (?, ?) AND introduced_at IS NOT NULL
        """,
        CARD_TASKS,
    ).fetchone()["n"]
    due_now = conn.execute(
        """
        SELECT COUNT(*) AS n FROM task_state
         WHERE task IN (?, ?) AND introduced_at IS NOT NULL AND due_at <= ?
        """,
        (*CARD_TASKS, now_iso),
    ).fetchone()["n"]
    # mastered = words where BOTH directions are mastered (interval >= threshold)
    mastered = conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
          SELECT word_id FROM task_state
           WHERE task IN (?, ?) AND interval_days >= ?
           GROUP BY word_id
          HAVING COUNT(DISTINCT task) = 2
        )
        """,
        (*CARD_TASKS, MASTERED_INTERVAL_DAYS),
    ).fetchone()["n"]

    recent = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(correct) AS right_count
          FROM reviews
         WHERE asked_at >= ?
        """,
        (seven_days_ago_iso,),
    ).fetchone()
    reviews_7d = recent["total"] or 0
    accuracy = (recent["right_count"] or 0) / reviews_7d if reviews_7d else None

    return Stats(
        total_words=total,
        introduced=introduced,
        due_now=due_now,
        mastered=mastered,
        reviews_last_7_days=reviews_7d,
        accuracy_last_7_days=accuracy,
    )


@router.get("/stats", response_model=Stats)
def get_stats(conn: sqlite3.Connection = Depends(get_conn)) -> Stats:
    """Aggregate counts used by the Stats view."""
    return _summary(conn)


def _overview(conn: sqlite3.Connection) -> StatsOverview:
    """Landing-page rollup: due lookahead + mastery distribution + 7-day
    efficiency. All counts are over the active (non-ignored) study set."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    hour_iso = (now + timedelta(hours=1)).isoformat()
    day_iso = (now + timedelta(hours=24)).isoformat()
    seven_days_ago_iso = (now - timedelta(days=7)).isoformat()

    total_words = conn.execute(
        "SELECT COUNT(*) AS n FROM words WHERE ignored_at IS NULL"
    ).fetchone()["n"]

    # Due lookahead — three cumulative windows over introduced recall lanes,
    # then differenced so due_now + next_hour + next_24h == total due in 24h.
    def _due_before(cutoff_iso: str) -> int:
        return conn.execute(
            """
            SELECT COUNT(*) AS n FROM task_state ts
              JOIN words w ON w.id = ts.word_id
             WHERE w.ignored_at IS NULL
               AND ts.task IN (?, ?)
               AND ts.introduced_at IS NOT NULL
               AND ts.due_at <= ?
            """,
            (*CARD_TASKS, cutoff_iso),
        ).fetchone()["n"]

    due_now = _due_before(now_iso)
    due_within_hour = _due_before(hour_iso)
    due_within_day = _due_before(day_iso)
    due = DueWindows(
        due_now=due_now,
        next_hour=max(0, due_within_hour - due_now),
        next_24h=max(0, due_within_day - due_now),
    )

    # New (never-introduced, non-ignored) words available to learn.
    new_available = conn.execute(
        """
        SELECT COUNT(*) AS n FROM words w
         WHERE w.ignored_at IS NULL
           AND NOT EXISTS (
             SELECT 1 FROM task_state ts
              WHERE ts.word_id = w.id AND ts.task IN (?, ?)
                AND ts.introduced_at IS NOT NULL
           )
        """,
        CARD_TASKS,
    ).fetchone()["n"]

    # Mastery distribution: classify each recall lane, take the word's stronger
    # direction, and average that word's ease across its introduced lanes.
    rows = conn.execute(
        """
        SELECT w.id AS word_id, ts.task, ts.ease, ts.interval_days,
               ts.repetitions, ts.introduced_at
          FROM words w
          LEFT JOIN task_state ts
            ON ts.word_id = w.id AND ts.task IN (?, ?)
         WHERE w.ignored_at IS NULL
        """,
        CARD_TASKS,
    ).fetchall()

    per_word: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        per_word.setdefault(r["word_id"], [])
        if r["task"] is not None:
            per_word[r["word_id"]].append(r)

    introduced = 0
    counts = {t: 0 for t in MATURITY_ORDER}
    ease_sum = {t: 0.0 for t in MATURITY_ORDER}
    ease_n = {t: 0 for t in MATURITY_ORDER}
    for lanes in per_word.values():
        word_intro = any(r["introduced_at"] is not None for r in lanes)
        if word_intro:
            introduced += 1
        maturity = "new"
        eases: list[float] = []
        for r in lanes:
            intro = r["introduced_at"] is not None
            m = classify_maturity(r["interval_days"], r["repetitions"], intro)
            if MATURITY_ORDER.index(m) > MATURITY_ORDER.index(maturity):
                maturity = m
            if intro:
                eases.append(r["ease"])
        counts[maturity] += 1
        if eases:
            ease_sum[maturity] += sum(eases) / len(eases)
            ease_n[maturity] += 1

    maturity = [
        MaturityTier(
            maturity=t,  # type: ignore[arg-type]
            count=counts[t],
            avg_ease=round(ease_sum[t] / ease_n[t], 2) if ease_n[t] else None,
        )
        for t in MATURITY_ORDER
    ]

    recent = conn.execute(
        """
        SELECT correct, latency_ms FROM reviews
         WHERE asked_at >= ?
        """,
        (seven_days_ago_iso,),
    ).fetchall()
    reviews_7d = len(recent)
    right = sum(1 for r in recent if r["correct"])
    accuracy = right / reviews_7d if reviews_7d else None
    recent_latencies = sorted(
        r["latency_ms"] for r in recent if r["correct"] and r["latency_ms"] is not None
    )
    median_ms = (
        int(recent_latencies[len(recent_latencies) // 2]) if recent_latencies else None
    )

    return StatsOverview(
        total_words=total_words,
        introduced=introduced,
        new_available=new_available,
        due=due,
        maturity=maturity,
        reviews_last_7_days=reviews_7d,
        accuracy_last_7_days=accuracy,
        median_latency_ms=median_ms,
    )


@router.get("/stats/overview", response_model=StatsOverview)
def get_overview(conn: sqlite3.Connection = Depends(get_conn)) -> StatsOverview:
    """Compact landing-page summary — due lookahead, mastery mix, efficiency."""
    return _overview(conn)


@router.get("/stats/detailed", response_model=DetailedStats)
def get_detailed_stats(conn: sqlite3.Connection = Depends(get_conn)) -> DetailedStats:
    """Latency histogram + per-word maturity, used by the Stats view drill-down."""
    summary = _summary(conn)

    latency_rows = conn.execute(
        """
        SELECT latency_ms FROM reviews
         WHERE latency_ms IS NOT NULL AND correct = 1
        """
    ).fetchall()
    latencies = [r["latency_ms"] for r in latency_rows]

    buckets: list[LatencyBucket] = []
    for lower, upper in LATENCY_BUCKETS_MS:
        if upper is None:
            count = sum(1 for v in latencies if v >= lower)
        else:
            count = sum(1 for v in latencies if lower <= v < upper)
        buckets.append(LatencyBucket(lower_ms=lower, upper_ms=upper, count=count))

    median_ms: int | None
    if latencies:
        ordered = sorted(latencies)
        median_ms = int(ordered[len(ordered) // 2])
    else:
        median_ms = None

    # Pull every word + its recall task_state rows + deck name in one go.
    word_rows = conn.execute(
        """
        SELECT w.id, w.kana, w.english, w.kanji, w.deck_id,
               d.name AS deck_name
          FROM words w
          LEFT JOIN decks d ON d.id = w.deck_id
         ORDER BY w.kana ASC
        """
    ).fetchall()
    cs_rows = conn.execute(
        """
        SELECT word_id, task, ease, interval_days, repetitions,
               due_at, introduced_at
          FROM task_state
         WHERE task IN (?, ?)
        """,
        CARD_TASKS,
    ).fetchall()
    cs_by_word: dict[int, dict[str, sqlite3.Row]] = {}
    for r in cs_rows:
        cs_by_word.setdefault(r["word_id"], {})[r["task"]] = r

    streaks = all_consecutive_failures(conn)

    words: list[WordState] = []
    maturity_counts: dict[str, int] = {
        "new": 0, "learning": 0, "young": 0, "mature": 0, "mastered": 0
    }

    def _direction_state(
        cs: sqlite3.Row | None, word_id: int, direction: str
    ) -> DirectionState:
        if cs is None:
            return DirectionState(
                ease=2.5,
                interval_days=0.0,
                repetitions=0,
                due_at=None,
                introduced_at=None,
                maturity="new",
                failure_streak=0,
                leech=False,
            )
        introduced = cs["introduced_at"] is not None
        maturity = classify_maturity(cs["interval_days"], cs["repetitions"], introduced)
        streak = streaks.get((word_id, direction), 0)
        return DirectionState(
            ease=cs["ease"],
            interval_days=cs["interval_days"],
            repetitions=cs["repetitions"],
            due_at=datetime.fromisoformat(cs["due_at"]) if cs["due_at"] else None,
            introduced_at=datetime.fromisoformat(cs["introduced_at"]) if introduced else None,
            maturity=maturity,  # type: ignore[arg-type]
            failure_streak=streak,
            leech=streak >= LEECH_FAILURE_THRESHOLD,
        )

    for row in word_rows:
        wid = row["id"]
        per_dir = cs_by_word.get(wid, {})
        en2ja = _direction_state(per_dir.get(TASK_EN2JA), wid, TASK_EN2JA)
        ja2en = _direction_state(per_dir.get(TASK_JA2EN), wid, TASK_JA2EN)
        # Top-level maturity = max of the two direction maturities.
        mat = max(
            (en2ja.maturity, ja2en.maturity),
            key=lambda m: MATURITY_ORDER.index(m),
        )
        maturity_counts[mat] += 1
        words.append(
            WordState(
                id=wid,
                kana=row["kana"],
                english=row["english"],
                kanji=row["kanji"],
                deck_id=row["deck_id"],
                deck_name=row["deck_name"],
                directions={TASK_EN2JA: en2ja, TASK_JA2EN: ja2en},
                maturity=mat,  # type: ignore[arg-type]
            )
        )

    # Sort by overall maturity desc, then kana asc — same intent as before
    # (most-mature on top) but driven by the combined bucket.
    words.sort(
        key=lambda w: (-MATURITY_ORDER.index(w.maturity), w.kana)
    )

    return DetailedStats(
        summary=summary,
        latency_histogram=buckets,
        median_latency_ms=median_ms,
        words=words,
        maturity_counts=maturity_counts,
    )


@router.get("/stats/debug")
def get_debug(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Cache + prefetch-worker telemetry for the Debug view.

    Shape is intentionally a free-form dict (no Pydantic model) so we can
    add fields without churning the schemas module. The view just renders
    the keys it knows about and ignores the rest.
    """
    path = db_path()
    try:
        size_bytes = path.stat().st_size if path.exists() else 0
    except OSError:
        size_bytes = 0

    audio_total = conn.execute("SELECT COUNT(*) AS n FROM audio_cache").fetchone()["n"]
    sentence_total = conn.execute(
        "SELECT COUNT(*) AS n FROM sentence_cache"
    ).fetchone()["n"]
    reviews_total = conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]

    sentence_by_model = [
        {"model": r["model"], "count": r["n"]}
        for r in conn.execute(
            "SELECT model, COUNT(*) AS n FROM sentence_cache GROUP BY model ORDER BY n DESC"
        ).fetchall()
    ]

    return {
        "db": {
            "path": str(path),
            "size_bytes": size_bytes,
        },
        "tables": {
            "words": conn.execute("SELECT COUNT(*) AS n FROM words").fetchone()["n"],
            "reviews": reviews_total,
            "sessions": conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"],
            "audio_cache": audio_total,
            "sentence_cache": sentence_total,
        },
        "audio": tts.prefetch_status(conn),
        "sentences": gemini.prefetch_status(conn),
        "sentence_cache_by_model": sentence_by_model,
    }


@router.get("/gemini_log")
def get_gemini_log(
    conn: sqlite3.Connection = Depends(get_conn),
    limit: int = Query(200, ge=1, le=1000),
    word_id: int | None = Query(None),
) -> dict:
    """Recent Gemini semantic-grading calls, newest first.

    Pairs each log row with the word's surface fields so the viewer can
    render meaningful labels without a second round-trip.
    """
    where = ""
    params: list[object] = []
    if word_id is not None:
        where = "WHERE g.word_id = ?"
        params.append(word_id)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT
          g.id, g.created_at, g.model, g.latency_ms, g.word_id,
          g.direction, g.typed_answer, g.cloze_sentence, g.cloze_target_form,
          g.verdict, g.explanation, g.alternates_json, g.clarified_gloss,
          g.error, g.final_correct,
          w.kanji AS word_kanji, w.kana AS word_kana, w.english AS word_english
        FROM gemini_grade_log g
        LEFT JOIN words w ON w.id = g.word_id
        {where}
        ORDER BY g.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    entries = []
    for r in rows:
        alts: list[str] = []
        if r["alternates_json"]:
            try:
                parsed = json.loads(r["alternates_json"])
                if isinstance(parsed, list):
                    alts = [str(x) for x in parsed]
            except json.JSONDecodeError:
                alts = []
        entries.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "model": r["model"],
            "latency_ms": r["latency_ms"],
            "word_id": r["word_id"],
            "word_kanji": r["word_kanji"],
            "word_kana": r["word_kana"],
            "word_english": r["word_english"],
            "direction": r["direction"],
            "typed_answer": r["typed_answer"],
            "cloze_sentence": r["cloze_sentence"],
            "cloze_target_form": r["cloze_target_form"],
            "verdict": r["verdict"],
            "explanation": r["explanation"],
            "alternates": alts,
            "clarified_gloss": r["clarified_gloss"],
            "error": r["error"],
            "final_correct": bool(r["final_correct"]),
        })
    return {"entries": entries}
