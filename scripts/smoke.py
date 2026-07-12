"""End-to-end smoke harness for the SPA.

Use this to catch regressions like the DecksView v-if/v-else bug:
silent runtime errors that the build doesn't surface and that
``vue-tsc --noEmit`` can't catch. Each enumerated route is opened in
headless Chromium against a freshly-built bundle, every page error is
captured, and any non-empty error set fails the run.

Run it after touching any view file:

    npm --prefix frontend run build       # if you haven't already
    uv run python scripts/smoke.py        # build is reused if present

Pass ``--rebuild`` to force a fresh ``npm run build`` first.  Pass
``--db PATH`` to point at an existing sqlite (the default is your
data/kana_quiz.sqlite so the run sees real-shaped data).  The harness
DOES NOT mutate the DB — every action is a GET or an idempotent
toggle that's reversed before tear-down.

Architecturally: spins up uvicorn on a free local port with
``KANA_AUTH_PASSWORD`` unset (so the API is open), points
``KANA_QUIZ_STATIC_DIR`` at ``frontend/dist``, and drives the browser.
Source maps are shipped with the prod build (see rsbuild.config.ts),
so reported stack frames resolve to original .vue / .ts lines.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "kana_quiz.sqlite"
DIST_DIR = ROOT / "frontend" / "dist"


@dataclass
class CaptureSink:
    """Bag for everything the page emitted while the harness was looking."""
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)

    def extend(self, other: "CaptureSink") -> None:
        self.page_errors.extend(other.page_errors)
        self.failed_requests.extend(other.failed_requests)
        self.console_errors.extend(other.console_errors)

    @property
    def empty(self) -> bool:
        return not (self.page_errors or self.failed_requests or self.console_errors)


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


@contextmanager
def _server(db_path: Path, dist: Path):
    """Run the backend in a subprocess for the duration of the with-block."""
    port = _free_port()
    env = dict(os.environ)
    env["KANA_QUIZ_DB"] = str(db_path)
    env["KANA_QUIZ_STATIC_DIR"] = str(dist)
    # Force the API gate off so the harness doesn't need to log in.
    env.pop("KANA_AUTH_PASSWORD", None)
    env.pop("KANA_AUTH_SECRET", None)
    proc = subprocess.Popen(
        [
            # `python -m uvicorn`, not the `uvicorn` console script — the latter
            # fails to spawn in this env (uv console-script shim is broken).
            "uv", "run", "python", "-m", "uvicorn", "kana_quiz.main:app",
            "--port", str(port), "--app-dir", "backend",
            "--log-level", "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_http(f"http://127.0.0.1:{port}/api/auth/status")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _attach(page, sink: CaptureSink) -> None:
    """Wire page-level event listeners that route into ``sink``."""
    def _on_console(msg):
        if msg.type == "error":
            sink.console_errors.append(msg.text)
    page.on("console", _on_console)
    page.on("pageerror", lambda e: sink.page_errors.append(repr(e)))
    def _on_failed(r):
        # ERR_ABORTED at teardown isn't a real failure — it's the browser
        # tearing down before in-flight fetches resolved. Anything else
        # (refused, dns, timeout) we still want to see.
        fail = (r.failure or "").lower()
        if "err_aborted" in fail:
            return
        sink.failed_requests.append(f"FAIL {r.method} {r.url} — {r.failure}")
    page.on("requestfailed", _on_failed)
    page.on(
        "response",
        lambda r: sink.failed_requests.append(
            f"{r.status} {r.request.method} {r.url}"
        ) if r.status >= 500 else None,
    )


def _route_smoke(page, base: str, path: str) -> CaptureSink:
    sink = CaptureSink()
    _attach(page, sink)
    page.goto(f"{base}{path}", wait_until="networkidle")
    page.wait_for_timeout(500)
    return sink


def _drive_deck_detail(page, base: str) -> CaptureSink:
    """Walk the new deck-detail flow end-to-end.

    Visits /decks, follows a Manage link, asserts the cards table
    renders, then exercises the ignore toggle (twice, to leave state
    untouched). Any console error or unexpected count fails the run.
    """
    sink = CaptureSink()
    _attach(page, sink)

    page.goto(f"{base}/decks", wait_until="networkidle")
    page.wait_for_selector(".deck-list .deck-card", timeout=8000)
    first_manage = page.locator(".deck-card a:has-text('Manage')").first
    if first_manage.count() == 0:
        sink.page_errors.append("no 'Manage' link visible on /decks")
        return sink
    first_manage.click()
    page.wait_for_url("**/decks/*", timeout=5000)
    page.wait_for_selector(".words-table tbody tr", timeout=8000)

    rows = page.locator(".words-table tbody tr")
    row_count = rows.count()
    if row_count == 0:
        sink.page_errors.append("deck-detail table rendered 0 rows")
        return sink

    # Snapshot a row's ignored state, toggle twice, assert it round-trips.
    first_row = rows.first
    first_toggle = first_row.locator(".toggle")
    was_on = first_toggle.get_attribute("aria-pressed") == "true"
    first_toggle.click()
    page.wait_for_load_state("networkidle")
    is_on_after_one = first_toggle.get_attribute("aria-pressed") == "true"
    if is_on_after_one == was_on:
        sink.page_errors.append(
            f"toggle didn't flip on first click (was {was_on}, still {is_on_after_one})"
        )
    first_toggle.click()
    page.wait_for_load_state("networkidle")
    is_on_after_two = first_toggle.get_attribute("aria-pressed") == "true"
    if is_on_after_two != was_on:
        sink.page_errors.append(
            f"toggle didn't restore on second click (was {was_on}, now {is_on_after_two})"
        )

    # Filter chips should narrow the table without throwing.
    page.locator(".chip:has-text('Ignored')").click()
    page.wait_for_timeout(200)
    page.locator(".chip:has-text('All')").click()
    page.wait_for_timeout(200)

    # Verify Kana sort: clicking the header once should produce a
    # specific order, clicking again should reverse it exactly, and
    # clicking a third time should clear it back to the server's
    # id-ASC order. Comparing asc vs desc avoids needing to replicate
    # the frontend's locale-aware Japanese collation here in Python.
    kanas_unsorted = page.locator(".words-table tbody tr .col-kana").all_text_contents()
    page.locator("th.sortable:has-text('Kana')").click()
    page.wait_for_timeout(200)
    kanas_asc = page.locator(".words-table tbody tr .col-kana").all_text_contents()
    if len(kanas_asc) >= 2 and kanas_asc == kanas_unsorted:
        sink.page_errors.append("kana sort asc didn't change row order")
    page.locator("th.sortable:has-text('Kana')").click()
    page.wait_for_timeout(200)
    kanas_desc = page.locator(".words-table tbody tr .col-kana").all_text_contents()
    if kanas_desc != list(reversed(kanas_asc)):
        sink.page_errors.append(
            "kana sort desc isn't reverse of asc — "
            f"asc[:3]={kanas_asc[:3]}, desc[:3]={kanas_desc[:3]}"
        )
    page.locator("th.sortable:has-text('Kana')").click()
    page.wait_for_timeout(200)
    kanas_cleared = page.locator(".words-table tbody tr .col-kana").all_text_contents()
    if kanas_cleared != kanas_unsorted:
        sink.page_errors.append("kana sort third click didn't restore original order")

    # Search should filter and clear cleanly.
    search = page.locator("input[type='search']")
    search.fill("zzznotamatch")
    page.wait_for_timeout(200)
    if page.locator(".words-table tbody tr").count() != 0:
        sink.page_errors.append("filter for unmatched query didn't empty the table")
    search.fill("")
    page.wait_for_timeout(200)

    return sink


def run(args: argparse.Namespace) -> int:
    if args.rebuild or not DIST_DIR.exists():
        print("building frontend…")
        subprocess.run(
            ["npm", "--prefix", str(ROOT / "frontend"), "run", "build"],
            check=True,
        )

    if not args.db.exists():
        print(f"error: db {args.db} not found", file=sys.stderr)
        return 2

    from playwright.sync_api import sync_playwright

    routes = ["/study", "/decks", "/stats", "/import", "/debug"]
    print(f"smoke harness: {len(routes)} routes + deck-detail flow")

    overall = CaptureSink()
    with _server(args.db, DIST_DIR) as base:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()

            for path in routes:
                sink = _route_smoke(page, base, path)
                tag = "OK" if sink.empty else "FAIL"
                print(f"  [{tag}] GET {path}")
                overall.extend(sink)

            print("  --- deck-detail flow ---")
            sink = _drive_deck_detail(page, base)
            tag = "OK" if sink.empty else "FAIL"
            print(f"  [{tag}] /decks → Manage → toggle/filter/search")
            overall.extend(sink)

            browser.close()

    if overall.empty:
        print("\nall routes clean.")
        return 0

    print("\nFAILURES:")
    for e in overall.page_errors:
        print(f"  pageerror: {e}")
    for c in overall.console_errors:
        print(f"  console:   {c}")
    for r in overall.failed_requests:
        print(f"  network:   {r}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true", help="force npm run build first")
    ap.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"sqlite path (default: {DEFAULT_DB.relative_to(ROOT)})",
    )
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
