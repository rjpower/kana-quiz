"""Replay a recorded fixture and diff every response against the record.

    uv run python scripts/replay_fixture.py fixtures/session.jsonl
    uv run python scripts/replay_fixture.py fixtures/session.jsonl --base-url http://127.0.0.1:8410 --token ...

Without ``--base-url`` the Python app runs in this process on a fresh copy
of ``<fixture>.sqlite`` with ``KANA_PINS=1``. With it, each request goes
over HTTP to a backend that honors the same pin headers. Either way the
exit status is the number of requests whose status or body differs, and
``--state <file>`` writes the final table state for a diff between the
two backends.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

STATE_TABLES = {
    "words": "SELECT * FROM words ORDER BY id",
    "decks": "SELECT * FROM decks ORDER BY id",
    "task_state": "SELECT * FROM task_state ORDER BY word_id, task",
    "reviews": "SELECT * FROM reviews ORDER BY id",
    "word_alternates": "SELECT * FROM word_alternates ORDER BY id",
    "sentence_cache": "SELECT word_id, model, japanese, english, mnemonic, target_form, source FROM sentence_cache ORDER BY word_id, model",
    "gemini_grade_log": "SELECT * FROM gemini_grade_log ORDER BY id",
}


def _body(entry: dict) -> bytes | None:
    raw = entry.get("body")
    if raw is None:
        return None
    if "text" in raw:
        return raw["text"].encode("utf-8")
    return base64.b64decode(raw["b64"])


def _decode(raw: dict | None) -> object:
    if raw is None:
        return None
    if "b64" in raw:
        return {"b64": raw["b64"]}
    text = raw["text"]
    try:
        return json.loads(text)
    except ValueError:
        return text


# Fields that are measured or environmental, so a replay never matches them.
IGNORED = {"$.db.path"}
IGNORED_KEYS = {"latency_ms"}


def _diff(path: str, want: object, got: object, out: list[str]) -> None:
    if path in IGNORED or path.rsplit(".", 1)[-1] in IGNORED_KEYS:
        return
    if isinstance(want, dict) and isinstance(got, dict):
        for key in sorted(set(want) | set(got)):
            if key not in want:
                out.append(f"{path}.{key}: extra {json.dumps(got[key], ensure_ascii=False)[:120]}")
            elif key not in got:
                out.append(f"{path}.{key}: missing")
            else:
                _diff(f"{path}.{key}", want[key], got[key], out)
        return
    if isinstance(want, list) and isinstance(got, list):
        if len(want) != len(got):
            out.append(f"{path}: {len(want)} items recorded, {len(got)} replayed")
        for i, (a, b) in enumerate(zip(want, got)):
            _diff(f"{path}[{i}]", a, b, out)
        return
    if want != got:
        out.append(f"{path}: {json.dumps(want, ensure_ascii=False)[:120]} -> {json.dumps(got, ensure_ascii=False)[:120]}")


def replay(send, entries: list[dict]) -> int:  # noqa: ANN001
    failures = 0
    for i, entry in enumerate(entries):
        headers = {"X-Kana-Now": entry["now"], "X-Kana-Seed": str(entry["seed"])}
        if entry["model"]:
            headers["X-Kana-Model"] = json.dumps(entry["model"], ensure_ascii=False)
        if entry.get("content_type"):
            headers["Content-Type"] = entry["content_type"]
        status, payload = send(entry["method"], entry["target"], headers, _body(entry))
        problems: list[str] = []
        if status != entry["status"]:
            problems.append(f"status {entry['status']} -> {status}")
        want = _decode(entry.get("response"))
        got = _decode({"text": payload.decode("utf-8")} if payload else None) if _is_text(payload) else {"b64": base64.b64encode(payload).decode()}
        _diff("$", want, got, problems)
        if problems:
            failures += 1
            print(f"#{i} {entry['method']} {entry['target']}")
            for line in problems[:12]:
                print(f"    {line}")
            if len(problems) > 12:
                print(f"    ... {len(problems) - 12} more")
    return failures


def _is_text(payload: bytes) -> bool:
    try:
        payload.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def dump_state(db: Path, out: Path) -> None:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    state = {
        name: [dict(r) for r in conn.execute(query).fetchall()] for name, query in STATE_TABLES.items()
    }
    out.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--token", help="bearer token for an HTTP backend")
    parser.add_argument("--state", type=Path, help="write the final table state here (in-process only)")
    args = parser.parse_args()
    entries = [json.loads(line) for line in args.fixture.open(encoding="utf-8")]

    if args.base_url:
        import httpx

        client = httpx.Client(base_url=args.base_url, timeout=60)

        def send(method: str, target: str, headers: dict, body: bytes | None):
            if args.token:
                headers = {**headers, "Authorization": f"Bearer {args.token}"}
            resp = client.request(method, target, headers=headers, content=body)
            return resp.status_code, resp.content

        failures = replay(send, entries)
    else:
        work = Path(tempfile.mkdtemp(prefix="kana-replay-"))
        db_copy = work / "kana_quiz.sqlite"
        shutil.copyfile(args.fixture.with_suffix(".sqlite"), db_copy)
        os.environ["KANA_QUIZ_DB"] = str(db_copy)
        os.environ["KANA_PINS"] = "1"
        os.environ["KANA_TTS_DISABLED"] = "1"
        os.environ["KANA_PREFETCH_DISABLED"] = "1"
        os.environ.pop("KANA_RECORD", None)
        os.environ.pop("KANA_AUTH_PASSWORD", None)
        os.environ.pop("KANA_API_TOKEN", None)
        from fastapi.testclient import TestClient

        from kana_quiz import main as main_module

        importlib.reload(main_module)
        with TestClient(main_module.app) as c:

            def send(method: str, target: str, headers: dict, body: bytes | None):
                resp = c.request(method, target, headers=headers, content=body)
                return resp.status_code, resp.content

            failures = replay(send, entries)
        if args.state:
            dump_state(db_copy, args.state)
        shutil.rmtree(work, ignore_errors=True)

    print(f"{len(entries)} requests replayed, {failures} differ")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
