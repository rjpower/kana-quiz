"""Playwright walk-through of cloze + listening question modes.

The original ``playwright_drive.py`` exercises the MC / type-in / intro
ramp on a fresh deck. This one shortcuts past the SRS staircase by
priming the test DB directly so we can see the new cloze and
sentence_listen surfaces — the ones the regular drive script never
reaches because they sit at the top of the rung ladder.

Strategy: spin up the server against a throwaway sqlite, import the
sample vocab, then write straight to ``card_state`` / ``sentence_cache``
/ ``audio_cache`` to fast-forward one target word per run. Each pass
ignores every OTHER word so the picker is forced to surface our seeded
card. The listening pass also sets
``KANA_LISTENING_INTERLEAVE_PROBABILITY=1`` so the interleaver fires
deterministically.

Run:
    uv run python scripts/playwright_modes.py
or with a visible browser:
    uv run python scripts/playwright_modes.py --headed --slow-mo 200
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOT_DIR = ROOT / "scripts" / "_pw_modes"
TMP_DB = ROOT / "data" / "kana_quiz_pw_modes.sqlite"
SAMPLE_CSV = ROOT / "sample_vocab.csv"

CLOZE_KANA = "いぬ"
CLOZE_JA_SENTENCE = "わたしのいぬはとてもかわいいです。"
CLOZE_TARGET_FORM = "いぬ"

LISTEN_KANA = "ねこ"
LISTEN_JA_SENTENCE = "ねこがそこにいます。"
LISTEN_EN_SENTENCE = "There is a cat over there."


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 20.0) -> None:
    import urllib.error
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"server didn't come up on {url}")


def _import_sample(base_url: str) -> None:
    import urllib.request
    boundary = "----pwboundary"
    with open(SAMPLE_CSV, "rb") as f:
        csv_bytes = f.read()
    parts = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="new_deck_name"\r\n\r\n'
            "pw-modes\r\n"
        ).encode(),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="vocab.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode()
        + csv_bytes
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{base_url}/api/import",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


def _ignore_all_words_except(db_path: Path, keep_ids: list[int]) -> None:
    """Mark every word ignored except those in ``keep_ids``. Lets the
    picker surface only our seeded card."""
    now_iso = "2020-01-01T00:00:00+00:00"
    placeholders = ",".join("?" for _ in keep_ids)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE words SET ignored_at = ? WHERE id NOT IN ({placeholders})",
            (now_iso, *keep_ids),
        )
        conn.execute(
            f"UPDATE words SET ignored_at = NULL WHERE id IN ({placeholders})",
            keep_ids,
        )
        conn.commit()
    finally:
        conn.close()


def _seed_cloze(db_path: Path) -> int:
    """Set the dog card to clearly above the cloze threshold + populate
    a cached sentence with a target_form that matches."""
    now_iso = "2020-01-01T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    try:
        word_id = conn.execute(
            "SELECT id FROM words WHERE kana = ?", (CLOZE_KANA,)
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO card_state (word_id, direction, ease, interval_days, repetitions, due_at, introduced_at)
            VALUES (?, 'en2ja', 2.85, 7.0, 6, ?, ?)
            ON CONFLICT(word_id, direction) DO UPDATE SET
              ease=excluded.ease, repetitions=excluded.repetitions,
              interval_days=excluded.interval_days, due_at=excluded.due_at,
              introduced_at=excluded.introduced_at
            """,
            (word_id, now_iso, now_iso),
        )
        conn.execute(
            """
            INSERT INTO sentence_cache (word_id, model, japanese, english, mnemonic, target_form, created_at)
            VALUES (?, 'gemini-3-flash-preview', ?, 'My dog is very cute.', '', ?, ?)
            ON CONFLICT(word_id, model) DO UPDATE SET
              japanese=excluded.japanese, english=excluded.english,
              target_form=excluded.target_form
            """,
            (word_id, CLOZE_JA_SENTENCE, CLOZE_TARGET_FORM, now_iso),
        )
        conn.commit()
        return word_id
    finally:
        conn.close()


def _seed_listening(db_path: Path) -> int:
    """Set the cat card past the listening eligibility floor and stage
    a placeholder sentence audio blob."""
    now_iso = "2020-01-01T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    try:
        word_id = conn.execute(
            "SELECT id FROM words WHERE kana = ?", (LISTEN_KANA,)
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO card_state (word_id, direction, ease, interval_days, repetitions, due_at, introduced_at)
            VALUES (?, 'en2ja', 2.6, 3.0, 3, ?, ?)
            ON CONFLICT(word_id, direction) DO UPDATE SET
              ease=excluded.ease, repetitions=excluded.repetitions,
              interval_days=excluded.interval_days, due_at=excluded.due_at,
              introduced_at=excluded.introduced_at
            """,
            (word_id, now_iso, now_iso),
        )
        conn.execute(
            """
            INSERT INTO sentence_cache (word_id, model, japanese, english, mnemonic, target_form, created_at)
            VALUES (?, 'gemini-3-flash-preview', ?, ?, '', ?, ?)
            ON CONFLICT(word_id, model) DO UPDATE SET
              japanese=excluded.japanese, english=excluded.english,
              target_form=excluded.target_form
            """,
            (word_id, LISTEN_JA_SENTENCE, LISTEN_EN_SENTENCE, LISTEN_KANA, now_iso),
        )
        # Cooked MP3 silent frames; not playable but the cache row needs to
        # exist for the picker's audio-cache join to qualify the card.
        silent_mp3 = bytes.fromhex("FFFB9000" + "00" * 96) * 6
        conn.execute(
            """
            INSERT OR IGNORE INTO audio_cache (text, voice, model, mime, blob, created_at)
            VALUES (?, 'nova', 'gpt-4o-mini-tts', 'audio/mpeg', ?, ?)
            """,
            (LISTEN_JA_SENTENCE, silent_mp3, now_iso),
        )
        conn.commit()
        return word_id
    finally:
        conn.close()


def _start_server(port: int, *, listening_prob: float = 0.2) -> subprocess.Popen:
    env = dict(os.environ)
    env["KANA_QUIZ_DB"] = str(TMP_DB)
    env["KANA_QUIZ_STATIC_DIR"] = str(ROOT / "frontend" / "dist")
    env.pop("KANA_AUTH_PASSWORD", None)
    # Skip Gemini calls in this script — we pre-seed every cache row.
    env.pop("GEMINI_API_KEY", None)
    env.pop("GOOGLE_API_KEY", None)
    env["KANA_LISTENING_INTERLEAVE_PROBABILITY"] = str(listening_prob)
    return subprocess.Popen(
        [
            "uv", "run", "uvicorn", "kana_quiz.main:app",
            "--port", str(port), "--app-dir", "backend",
            "--log-level", "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def shot(page, name: str) -> None:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"  shot → {path.relative_to(ROOT)}")


def _drive_cloze_session(base: str, headless: bool, slow_mo: int) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
        ctx = browser.new_context(viewport={"width": 480, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda err: print(f"  [pageerror] {err}"))

        print("→ /study (cloze pass)")
        page.goto(f"{base}/study")
        page.wait_for_selector("text=Start round", timeout=10000)
        shot(page, "10_cloze_pre_round")
        page.click("text=Start round")

        # Cloze input is rendered via ClozeInput.vue → .cloze-form
        page.wait_for_selector(".cloze-form .cloze-input", timeout=10000)
        shot(page, "11_cloze_blank")

        # First answer: deliberately wrong so we get a lingering reveal
        # state to screenshot (correct answers auto-advance in ~200ms).
        cloze_input = page.locator(".cloze-form .cloze-input").first
        cloze_input.click()
        page.keyboard.type("neko")  # wanakana → ねこ — wrong word
        page.wait_for_timeout(500)
        shot(page, "12_cloze_typed_wrong")
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        shot(page, "13_cloze_locked_incorrect")

        # Advance, then answer correctly. The auto-advance after a
        # correct verdict is fast (~200ms), so capture mid-flash.
        page.keyboard.press("Space")
        page.wait_for_timeout(600)
        cloze_input = page.locator(".cloze-form .cloze-input").first
        cloze_input.click()
        page.keyboard.type("inu")
        page.wait_for_timeout(300)
        shot(page, "14_cloze_typed_correct")
        page.keyboard.press("Enter")
        # Race a screenshot before auto-advance kicks. 150ms < 200ms flash.
        page.wait_for_timeout(150)
        shot(page, "15_cloze_locked_correct")

        browser.close()


def _drive_listening_session(base: str, headless: bool, slow_mo: int) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
        ctx = browser.new_context(viewport={"width": 480, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda err: print(f"  [pageerror] {err}"))

        print("→ /study (listening pass)")
        page.goto(f"{base}/study")
        page.wait_for_selector("text=Start round", timeout=10000)
        shot(page, "20_listen_pre_round")
        page.click("text=Start round")

        try:
            page.wait_for_selector(".dictation-form", timeout=10000)
            shot(page, "21_listen_card")
        except Exception:
            shot(page, "21_listen_missing")
            print("  WARN: dictation-form never rendered")
            browser.close()
            return

        # Wrong answer first so the reveal lingers and we can screenshot.
        listen_input = page.locator(".dictation-form input").first
        listen_input.click()
        page.keyboard.type("the moon is bright tonight")
        page.wait_for_timeout(500)
        shot(page, "22_listen_typed_wrong")
        page.keyboard.press("Enter")
        # No Gemini key in this run → grade_translation raises
        # GeminiUnavailable, the caller falls through to a deterministic
        # normalize compare. "moon is bright" vs "there is a cat" → wrong.
        page.wait_for_timeout(1200)
        shot(page, "23_listen_locked_incorrect")

        page.keyboard.press("Space")
        page.wait_for_timeout(600)
        listen_input = page.locator(".dictation-form input").first
        listen_input.click()
        # Fuzzy threshold for translations is 60%; an exact match
        # short-circuits to correct without any LLM call.
        page.keyboard.type("There is a cat over there.")
        page.wait_for_timeout(300)
        shot(page, "24_listen_typed_correct")
        page.keyboard.press("Enter")
        page.wait_for_timeout(150)
        shot(page, "25_listen_locked_correct")

        browser.close()


def _run_one(
    seed_fn,
    *,
    listening_prob: float,
    keep_ids: list[int] | None,
    driver,
    label: str,
    headless: bool,
    slow_mo: int,
) -> None:
    """Spin a fresh server (with optional listening-prob override),
    re-import the sample CSV against a fresh DB, run the seed, drive."""
    if TMP_DB.exists():
        TMP_DB.unlink()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server = _start_server(port, listening_prob=listening_prob)
    try:
        _wait_http(f"{base}/api/auth/status")
        _import_sample(base)
        seed_id = seed_fn(TMP_DB)
        _ignore_all_words_except(TMP_DB, [seed_id])
        print(f"[{label}] server up on {base}; seed word_id={seed_id}")
        driver(base, headless, slow_mo)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def drive(headless: bool, slow_mo: int) -> None:
    if SHOT_DIR.exists():
        shutil.rmtree(SHOT_DIR)

    _run_one(
        _seed_cloze,
        listening_prob=0.0,  # never interleave listening during cloze pass
        keep_ids=None,
        driver=_drive_cloze_session,
        label="cloze",
        headless=headless,
        slow_mo=slow_mo,
    )
    _run_one(
        _seed_listening,
        listening_prob=1.0,  # always interleave during listening pass
        keep_ids=None,
        driver=_drive_listening_session,
        label="listen",
        headless=headless,
        slow_mo=slow_mo,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--slow-mo", type=int, default=0)
    args = ap.parse_args()
    drive(headless=not args.headed, slow_mo=args.slow_mo)


if __name__ == "__main__":
    main()
