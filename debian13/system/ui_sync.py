"""Durable, per-installation UI synchronization for the private Debian 13 console.

No AI actions are fabricated here. Only explicitly recognized operator
commands update state; an actual agent transport can be connected separately.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_TITLE = "Dialog Triady"
TITLE_LIMIT = 80
MAX_EVENTS = 100
COMMAND = re.compile(r"^/(?:panel|ui)\s+(?:tytuł|tytul|title)\s+(.+)$", re.IGNORECASE | re.DOTALL)


class SyncValidationError(ValueError):
    pass


def parse_title_command(message: str) -> str | None:
    """Return requested title for a deterministic slash command, or None."""
    if not isinstance(message, str):
        return None
    stripped = message.strip()
    match = COMMAND.fullmatch(stripped)
    if match:
        return match.group(1).strip()
    if stripped.lower().startswith(("/panel", "/ui")):
        raise SyncValidationError("Use /panel tytuł NOWA NAZWA (1–80 znaków).")
    return None


def validate_title(value: str) -> str:
    if not isinstance(value, str):
        raise SyncValidationError("Tytuł musi być tekstem.")
    title = value.strip()
    if not 1 <= len(title) <= TITLE_LIMIT or any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise SyncValidationError("Tytuł musi mieć 1–80 znaków i nie może zawierać znaków kontrolnych.")
    return title


class SyncStore:
    def __init__(self, workspace: Path):
        self.path = Path(workspace) / ".debian13-ui-sync.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise SyncValidationError("Odłącz symlink bazy synchronizacji.")
        with self._open() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ui_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    title TEXT NOT NULL, revision INTEGER NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS ui_events (
                    revision INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                    source TEXT NOT NULL, label TEXT NOT NULL, created_at REAL NOT NULL);
                INSERT OR IGNORE INTO ui_state(id,title,revision,updated_at)
                    VALUES(1,'Dialog Triady',0,0);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _open(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def snapshot(self) -> dict:
        with self._open() as db:
            row = db.execute("SELECT title, revision, updated_at FROM ui_state WHERE id=1").fetchone()
            events = db.execute(
                "SELECT revision,kind,source,label,created_at FROM ui_events "
                "ORDER BY revision DESC LIMIT 20").fetchall()
        return {
            "title": row["title"], "revision": row["revision"],
            "updatedAt": row["updated_at"], "events": [dict(x) for x in events],
            "storage": "sqlite", "realAgentActionVerified": False,
        }

    def set_title(self, value: str) -> dict:
        title = validate_title(value)
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT title,revision FROM ui_state WHERE id=1").fetchone()
            if row["title"] != title:
                revision = row["revision"] + 1
                stamp = time.time()
                db.execute(
                    "UPDATE ui_state SET title=?,revision=?,updated_at=? WHERE id=1",
                    (title, revision, stamp))
                db.execute(
                    "INSERT INTO ui_events(revision,kind,source,label,created_at) "
                    "VALUES(?,'ui_title_changed','operator_command',?,?)",
                    (revision, title, stamp))
                db.execute("DELETE FROM ui_events WHERE revision <= ?",
                           (revision - MAX_EVENTS,))
            db.commit()
        return self.snapshot()
