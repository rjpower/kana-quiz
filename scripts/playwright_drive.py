"""Playwright harness for hands-on iteration on the kana-quiz UI.

Why this exists: Claude is configuring the new sentence + mnemonic auto-
generation flow and needs a way to actually observe the UI under various
states (new-card intro, wrong-answer reveal, round summary expansion)
without poking at it manually. This script:

  * spins up the FastAPI app against an isolated sqlite file,
  * imports `sample_vocab.csv` so there's a known deck,
  * launches a headless Chromium and walks through study scenarios,
  * takes screenshots into ``scripts/_pw_shots/`` for review.

It deliberately does NOT use the real prod sqlite — that one's been
backed up but should stay untouched. The temp DB is wiped on every run
so each pass starts with a fresh "new card" state.

Run:
    uv run python scripts/playwright_drive.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOT_DIR = ROOT / "scripts" / "_pw_shots"
TMP_DB = ROOT / "data" / "kana_quiz_pw.sqlite"
SAMPLE_CSV = ROOT / "sample_vocab.csv"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 15.0) -> None:
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
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="vocab.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + f.read() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{base_url}/api/import",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)


def _start_server(port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["KANA_QUIZ_DB"] = str(TMP_DB)
    env["KANA_QUIZ_STATIC_DIR"] = str(ROOT / "frontend" / "dist")
    # Don't gate the API behind a password for the drive.
    env.pop("KANA_AUTH_PASSWORD", None)
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


def drive(headless: bool, slow_mo: int) -> None:
    from playwright.sync_api import sync_playwright

    if TMP_DB.exists():
        TMP_DB.unlink()
    if SHOT_DIR.exists():
        shutil.rmtree(SHOT_DIR)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server = _start_server(port)
    try:
        _wait_http(f"{base}/api/auth/status")
        _import_sample(base)
        print(f"server up on {base}; sample vocab imported")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
            ctx = browser.new_context(viewport={"width": 420, "height": 820})
            page = ctx.new_page()

            page.on("console", lambda msg: print(f"  [console:{msg.type}] {msg.text}"))
            page.on("pageerror", lambda err: print(f"  [pageerror] {err}"))

            print("→ /study")
            page.goto(f"{base}/study")
            page.wait_for_selector("text=Ready to study?", timeout=5000)
            shot(page, "01_pre_round")

            print("→ click Start round")
            page.click("text=Start round")

            # First question is a brand-new word — intro card should appear.
            page.wait_for_selector(".intro-badge", timeout=10000)
            # Give the background prefetch up to ~12s to finish before the
            # screenshot, otherwise we're just capturing the spinner.
            try:
                page.wait_for_selector(".mnemonic", timeout=15000)
            except Exception:
                print("  WARN: mnemonic didn't render within 15s")
            shot(page, "02_intro_with_mnemonic")

            wrong_reveal_captured = False
            correct_reveal_captured = False

            # Walk a full round answering deterministically wrong (always
            # pick index 0; correct_index is uniform 0..3 so ~75% wrong).
            # Each tick: handle intro card, click first choice, wait, then
            # capture screenshots when we see distinctive UI states.
            for i in range(60):
                if page.locator(".summary").count() > 0:
                    break
                # New-word intro: dismiss after waiting for mnemonic.
                if page.locator(".intro-badge").count() > 0:
                    try:
                        page.wait_for_selector(".intro .mnemonic", timeout=8000)
                    except Exception:
                        pass
                    page.click("text=Got it")
                    page.wait_for_timeout(200)
                    continue
                # Active question.
                grid = page.locator(".grid > *")
                if grid.count() > 0:
                    grid.first.click()
                    # Wait for the lock state to settle (kanji-reveal +
                    # auto-fetched mnemonic if wrong).
                    page.wait_for_timeout(1500)
                    # Wrong-reveal: mnemonic auto-renders.
                    if (
                        not wrong_reveal_captured
                        and page.locator(".sentence-row .mnemonic").count() > 0
                    ):
                        shot(page, "03_wrong_reveal_with_mnemonic")
                        wrong_reveal_captured = True
                    # Correct: "Show example" button is visible.
                    elif (
                        not correct_reveal_captured
                        and page.locator(".sentence-row .btn.ghost").count() > 0
                    ):
                        shot(page, "04_correct_reveal_button")
                        page.locator(".sentence-row .btn.ghost").click()
                        try:
                            page.wait_for_selector(
                                ".sentence-row .mnemonic", timeout=12000
                            )
                            shot(page, "05_correct_reveal_loaded")
                        except Exception:
                            pass
                        correct_reveal_captured = True
                    # Skip the rest of the FLASH_MS_WRONG dwell using the
                    # Space shortcut so the script doesn't wait 4s per miss.
                    page.keyboard.press("Space")
                    page.wait_for_timeout(300)
                else:
                    page.wait_for_timeout(400)

            try:
                page.wait_for_selector(".summary", timeout=20000)
                shot(page, "06_round_summary")
            except Exception:
                print("  WARN: round summary didn't appear")
                shot(page, "06_no_summary")

            # Expand a missed row if any.
            miss_rows = page.locator(".miss-row")
            if miss_rows.count() > 0:
                miss_rows.first.click()
                try:
                    page.wait_for_selector(".miss-detail .mnemonic", timeout=12000)
                except Exception:
                    pass
                shot(page, "07_miss_expanded")

            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="run with a visible browser")
    ap.add_argument("--slow-mo", type=int, default=0, help="slow each action by N ms")
    args = ap.parse_args()
    drive(headless=not args.headed, slow_mo=args.slow_mo)


if __name__ == "__main__":
    main()
