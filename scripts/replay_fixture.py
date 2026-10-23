"""Replay a recorded fixture and diff every response against the record.

    uv run python scripts/replay_fixture.py fixtures/session.jsonl
    uv run python scripts/replay_fixture.py fixtures/session.jsonl --base-url http://127.0.0.1:8410 --token ...
    uv run python scripts/replay_fixture.py fixtures/session.jsonl --hyperbase http://kana.localhost:8410 \
        --username nao --password ... --pg-url postgres://... --state ts-state.json

Without ``--base-url`` the Python app runs in this process on a fresh copy
of ``<fixture>.sqlite`` with ``KANA_PINS=1``. With ``--base-url`` each
request goes over HTTP to a Python backend that honors the same pin
headers. With ``--hyperbase`` each request is mapped onto the site's op
(``POST /api/hb/v1/kana/<verb>`` with the pins inside the input) and the
op's answer is read back into the Python's shape before the comparison.
Either way the exit status is 1 when any request differs, and ``--state
<file>`` writes the final table state for a diff between the two backends.
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

# The columns both backends hold, in one order, so the two dumps compare.
STATE_TABLES = {
    "words": "SELECT id, kana, english, kanji, tags, deck_id, ignored_at, kind, archived_at FROM words ORDER BY id",
    "decks": "SELECT id, name, level, created_at, profile, pick_order FROM decks ORDER BY id",
    "task_state": "SELECT word_id, task, ease, interval_days, repetitions, due_at, introduced_at FROM task_state ORDER BY word_id, task",
    "reviews": "SELECT id, word_id, asked_at, direction, correct, timed_out, latency_ms, outcome FROM reviews ORDER BY id",
    "word_alternates": "SELECT id, word_id, direction, alternate, source, added_at FROM word_alternates ORDER BY id",
    "sentence_cache": "SELECT word_id, model, japanese, english, mnemonic, target_form, source FROM sentence_cache ORDER BY word_id, model",
    "gemini_grade_log": "SELECT id, created_at, model, word_id, direction, typed_answer, cloze_sentence, cloze_target_form, verdict, explanation, alternates_json, clarified_gloss, error, final_correct FROM gemini_grade_log ORDER BY id",
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


def _one(send, i: int, entry: dict) -> bool:  # noqa: ANN001
    """Send one recorded request and print what differs. True when nothing does."""
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
        print(f"#{i} {entry['method']} {entry['target']}")
        for line in problems[:12]:
            print(f"    {line}")
        if len(problems) > 12:
            print(f"    ... {len(problems) - 12} more")
    return not problems


def replay(send, entries: list[dict]) -> int:  # noqa: ANN001
    return sum(0 if _one(send, i, entry) else 1 for i, entry in enumerate(entries))


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


# ---------------------------------------------------------------------------
# the fixture's requests as the site's ops
# ---------------------------------------------------------------------------


def _multipart(content_type: str, body: bytes) -> dict[str, str]:
    """The fields of one multipart form, each decoded as UTF-8."""
    import email.parser
    import email.policy

    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = email.parser.BytesParser(policy=email.policy.default).parsebytes(head + body)
    fields: dict[str, str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            payload = part.get_payload(decode=True)
            fields[str(name)] = payload.decode("utf-8-sig") if payload else ""
    return fields


def _int_or_none(raw: str | None) -> int | None:
    return int(raw) if raw not in (None, "") else None


def as_op(entry: dict) -> tuple[str, dict] | None:
    """The op and input one recorded request maps onto, or ``None`` for a
    request the site answers another way (the sign-in status)."""
    from urllib.parse import parse_qs, urlsplit

    method = entry["method"]
    split = urlsplit(entry["target"])
    path = split.path
    q = {k: v[0] for k, v in parse_qs(split.query, keep_blank_values=True).items()}
    raw = _body(entry)
    body = json.loads(raw) if raw and (entry.get("content_type") or "").startswith("application/json") else {}
    parts = path.split("/")

    if path == "/api/auth/status":
        return None
    if path == "/api/glosses":
        return "kana.glosses", {}
    if path == "/api/words/quick":
        return "kana.word.quick", body
    if len(parts) == 5 and parts[2] == "words" and parts[4] == "ignore":
        return "kana.word.ignore", {"id": int(parts[3]), "ignored": method == "POST"}
    if len(parts) == 5 and parts[2] == "words" and parts[4] == "sentence":
        return "kana.word.sentence", {"id": int(parts[3])}
    if path == "/api/client_log":
        return "kana.client_log", body
    if path == "/api/stats":
        return "kana.stats", {}
    if path == "/api/stats/overview":
        return "kana.stats.overview", {"tz_offset": int(q.get("tz_offset", "0"))}
    if path == "/api/stats/detailed":
        return "kana.stats.detailed", {}
    if path == "/api/stats/debug":
        return "kana.stats.debug", {}
    if path == "/api/gemini_log":
        return "kana.grader_log", {"limit": int(q.get("limit", "50"))}
    if path == "/api/import":
        fields = _multipart(entry["content_type"], raw or b"")
        return "kana.import", {
            "csv": fields.get("file", ""),
            "deck_id": _int_or_none(fields.get("deck_id")),
            "new_deck_name": fields.get("new_deck_name") or None,
            "new_deck_level": _int_or_none(fields.get("new_deck_level")),
            "new_deck_profile": fields.get("new_deck_profile") or None,
        }
    if path == "/api/session/next":
        return "kana.session.next", dict(q)
    if path == "/api/session/batch":
        return "kana.session.batch", dict(q)
    if path == "/api/session/drill":
        return "kana.session.drill", {
            "word_id": int(q["word_id"]),
            "direction": q.get("direction"),
            "pool_ids": q.get("pool_ids"),
        }
    if path == "/api/session/upcoming":
        return "kana.session.upcoming", {"n": int(q.get("n", "5"))}
    if path == "/api/session/match-batch":
        return "kana.session.match_batch", {"n": int(q.get("n", "8")), "exclude_ids": q.get("exclude_ids")}
    if path == "/api/session/answer":
        return "kana.session.answer", body
    if path == "/api/decks" and method == "GET":
        return "kana.deck.list", {}
    if path == "/api/decks" and method == "POST":
        return "kana.deck.create", body
    if len(parts) == 4 and parts[2] == "decks" and method == "PATCH":
        return "kana.deck.update", {"id": int(parts[3]), **body}
    if len(parts) == 4 and parts[2] == "decks" and method == "DELETE":
        return "kana.deck.delete", {"id": int(parts[3]), "force": q.get("force") == "true"}
    if len(parts) == 5 and parts[2] == "decks" and parts[4] == "words":
        return "kana.deck.words", {"id": int(parts[3])}
    if len(parts) == 5 and parts[2] == "decks" and parts[4] == "ignored":
        return "kana.deck.ignored", {"id": int(parts[3])}
    if len(parts) == 5 and parts[2] == "audio":
        return f"kana.audio.{parts[3]}", {"id": int(parts[4])}
    raise ValueError(f"no op for {method} {path}")


def as_route_answer(entry: dict, status: int, payload: bytes) -> tuple[int, bytes]:
    """An op's answer in the shape the Python route wrote, so one diff reads both.

    A 204 route answers ``null``; a fault's ``message`` is the route's
    ``detail``; an audio op answers ``{mime, b64}`` where the route sent bytes.
    """
    if status >= 400:
        try:
            fault = json.loads(payload)["error"]
            return status, json.dumps({"detail": fault["message"]}, ensure_ascii=False).encode()
        except (ValueError, KeyError, TypeError):
            return status, payload
    if entry["status"] == 204 and payload.strip() == b"null":
        return 204, b""
    if entry["target"].startswith("/api/audio/"):
        answered = json.loads(payload)
        return status, base64.b64decode(answered["b64"])
    return status, payload


def sign_in(base_url: str, username: str, password: str) -> str:
    import httpx

    resp = httpx.post(
        f"{base_url}/api/hb/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(f"sign-in failed: {resp.status_code} {resp.text[:200]}")
    cookie = resp.headers.get("set-cookie", "").split(";")[0]
    if not cookie:
        raise SystemExit("the sign-in set no cookie")
    return cookie


def dump_state_pg(url: str, out: Path) -> None:
    """The same tables as :func:`dump_state`, read from Postgres through psql."""
    import subprocess

    state = {}
    for name, query in STATE_TABLES.items():
        wrapped = f"SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) FROM ({query}) t"
        got = subprocess.run(
            ["psql", url, "-Atc", wrapped], check=True, capture_output=True, text=True
        ).stdout
        state[name] = json.loads(got)
    out.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--base-url", help="a Python backend over HTTP")
    parser.add_argument("--token", help="bearer token for the Python backend")
    parser.add_argument("--hyperbase", help="the site's origin, for example http://kana.localhost:8410")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--pg-url", help="the site's database, for --state with --hyperbase")
    parser.add_argument("--state", type=Path, help="write the final table state here")
    args = parser.parse_args()
    entries = [json.loads(line) for line in args.fixture.open(encoding="utf-8")]

    if args.hyperbase:
        import httpx

        cookie = sign_in(args.hyperbase, args.username or "", args.password or "")
        client = httpx.Client(base_url=args.hyperbase, timeout=120, headers={"cookie": cookie})

        def send(method: str, target: str, headers: dict, body: bytes | None):
            entry = _current[0]
            mapped = as_op(entry)
            if mapped is None:
                return entry["status"], _body(entry) or b""
            op, op_input = mapped
            op_input = {
                **op_input,
                "pins": {
                    "now": entry["now"],
                    "seed": entry["seed"],
                    "model": entry["model"] or None,
                },
            }
            resp = client.post(f"/api/hb/v1/{op.split('.', 1)[0]}/{op.split('.', 1)[1]}", json=op_input)
            return as_route_answer(entry, resp.status_code, resp.content)

        _current: list[dict] = [{}]

        def replay_ops() -> int:
            failures = 0
            for i, entry in enumerate(entries):
                _current[0] = entry
                failures += 1 if not _one(send, i, entry) else 0
            return failures

        failures = replay_ops()
        if args.state:
            if not args.pg_url:
                raise SystemExit("--state with --hyperbase needs --pg-url")
            dump_state_pg(args.pg_url, args.state)
    elif args.base_url:
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
