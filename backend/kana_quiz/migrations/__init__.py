"""Lightweight homegrown migration runner.

Migrations live next to this module as files named ``NNN_<slug>.py`` where
``NNN`` is a zero-padded 3-digit version. Each module exposes ``up(conn)``
which mutates the schema. The runner records applied versions in
``schema_migrations`` and is idempotent — re-running on an up-to-date DB
performs no work.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_MIGRATION_RE = re.compile(r"^(\d{3})_[A-Za-z0-9_]+\.py$")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )


def _applied(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(r[0]) for r in rows}


def _discover() -> list[tuple[int, Path]]:
    here = Path(__file__).parent
    out: list[tuple[int, Path]] = []
    for entry in here.iterdir():
        if not entry.is_file():
            continue
        m = _MIGRATION_RE.match(entry.name)
        if m is None:
            continue
        out.append((int(m.group(1)), entry))
    out.sort(key=lambda t: t[0])
    return out


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(
        f"kana_quiz.migrations._{path.stem}", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load migration {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply every unapplied migration in version order.

    Each migration runs in its own transaction. The connection is expected
    to be in autocommit mode (``isolation_level=None``); we drive
    BEGIN/COMMIT/ROLLBACK explicitly.
    """
    _ensure_table(conn)
    done = _applied(conn)
    for version, path in _discover():
        if version in done:
            continue
        mod = _load(path)
        if not hasattr(mod, "up"):
            raise RuntimeError(f"migration {path.name} has no up()")
        log.info("applying migration %03d (%s)", version, path.name)
        conn.execute("BEGIN")
        try:
            mod.up(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            log.exception("migration %03d failed", version)
            raise
