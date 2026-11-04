"""Private SQLite history and explicitly saved operator notes for Dialog Triady.

This is application-local memory, not ChatGPT account memory. No image bytes,
API tokens or credentials are stored. All content is operator-controlled.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_CONTEXT_MESSAGES = 24
MAX_HISTORY_MESSAGES = 300
MAX_NOTES = 40
MAX_NOTE_LENGTH = 600
MAX_DIALOG_MESSAGES = 2000
DIALOG_AGENTS = frozenset({"operator", "aria", "alex", "astyria", "aurelius"})


class ChatMemory:
    def __init__(self, workspace: Path):
        self.path = Path(workspace) / "dialog_triady.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                agent TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_dialog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL, recipient TEXT NOT NULL,
                content TEXT NOT NULL, created_at REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_agent_dialog_recipient ON agent_dialog(recipient, id)")
            db.execute("""CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL UNIQUE, created_at REAL NOT NULL)""")
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 10000")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def exists(self, session_id: str) -> bool:
        with self._connection() as db:
            return db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is not None

    def context(self, session_id: str | None) -> list[dict[str, str]]:
        if not session_id:
            return []
        with self._connection() as db:
            rows = db.execute("""SELECT role, agent, content FROM messages
                WHERE session_id=? ORDER BY id DESC LIMIT ?""",
                (session_id, MAX_CONTEXT_MESSAGES)).fetchall()
        return [{"role": row["role"],
                 "content": (f"[{row['agent']}] " if row["role"] == "assistant" else "") + row["content"]}
                for row in reversed(rows)]

    def save_exchange(self, session_id: str | None, agent: str, message: str,
                      response: str, source: str, image_attached: bool = False) -> str:
        session_id = session_id or str(uuid.uuid4())
        now = time.time()
        with self._connection() as db:
            db.execute("""INSERT OR IGNORE INTO sessions(id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)""", (session_id, message[:64], now, now))
            user_text = message + (" [dołączono obraz; plik nie jest zapisywany]" if image_attached else "")
            db.execute("""INSERT INTO messages(session_id, agent, role, content, source, created_at)
                VALUES (?, ?, 'user', ?, 'operator', ?)""", (session_id, agent, user_text, now))
            db.execute("""INSERT INTO messages(session_id, agent, role, content, source, created_at)
                VALUES (?, ?, 'assistant', ?, ?, ?)""", (session_id, agent, response, source, now))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            db.execute("""DELETE FROM messages WHERE session_id=? AND id NOT IN
                (SELECT id FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?)""",
                (session_id, session_id, MAX_HISTORY_MESSAGES))
        return session_id

    def get_messages(self, session_id: str) -> list[dict] | None:
        with self._connection() as db:
            if db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                return None
            rows = db.execute("""SELECT agent, role, content, source, created_at
                FROM messages WHERE session_id=? ORDER BY id""", (session_id,)).fetchall()
            return [dict(row) for row in rows]

    def post_agent_dialog(self, sender: str, recipient: str, content: str) -> dict:
        """Persist a voluntary message. Caller must authenticate the sender.

        This method never invokes an AI model or automatically generates replies.
        """
        valid_recipients = DIALOG_AGENTS | {"all"}
        if sender not in DIALOG_AGENTS or recipient not in valid_recipients:
            raise ValueError("Unknown dialog participant.")
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 4000:
            raise ValueError("Message must have 1-4000 characters.")
        clean = content.strip()
        if any(ord(char) == 0 for char in clean):
            raise ValueError("NUL characters are not accepted.")
        now = time.time()
        with self._connection() as db:
            cur = db.execute(
                "INSERT INTO agent_dialog(sender,recipient,content,created_at) VALUES (?,?,?,?)",
                (sender, recipient, clean, now))
            # Bound local storage without deleting recently visible messages.
            db.execute(
                "DELETE FROM agent_dialog WHERE id <= (SELECT COALESCE(MAX(id),0) - ? FROM agent_dialog)",
                (MAX_DIALOG_MESSAGES,))
            return {"id": cur.lastrowid, "sender": sender, "recipient": recipient,
                    "content": clean, "created_at": now, "source": "authenticated_api"}

    def get_agent_dialog(self, audience: str, after: int = 0, limit: int = 100) -> list[dict]:
        """Only show an agent broadcasts, its own messages and messages to it.

        The operator can audit all conversations. This is message transport,
        NOT a runtime-inference or model-execution verification endpoint.
        """
        if audience not in DIALOG_AGENTS or not 0 <= after < 2**63 or not 1 <= limit <= 100:
            raise ValueError("Invalid dialog query.")
        with self._connection() as db:
            if audience == "operator":
                rows = db.execute(
                    "SELECT id,sender,recipient,content,created_at FROM agent_dialog "
                    "WHERE id>? ORDER BY id ASC LIMIT ?", (after, limit)).fetchall()
            else:
                rows = db.execute(
                    "SELECT id,sender,recipient,content,created_at FROM agent_dialog "
                    "WHERE id>? AND (recipient='all' OR recipient=? OR sender=?) "
                    "ORDER BY id ASC LIMIT ?", (after, audience, audience, limit)).fetchall()
        return [{**dict(row), "source": "authenticated_api"} for row in rows]

    def sessions(self) -> list[dict]:
        with self._connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, title, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 50")]

    def notes(self) -> list[dict]:
        with self._connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, content, created_at FROM memories ORDER BY id DESC LIMIT ?", (MAX_NOTES,))]

    def add_note(self, content: str) -> dict:
        content = content.strip()
        if not content or len(content) > MAX_NOTE_LENGTH:
            raise ValueError("A saved memory must have 1-600 characters.")
        with self._connection() as db:
            if db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] >= MAX_NOTES:
                raise ValueError("Memory is full; delete a note to add another.")
            try:
                cur = db.execute("INSERT INTO memories(content, created_at) VALUES (?, ?)",
                                 (content, time.time()))
            except sqlite3.IntegrityError as exc:
                raise ValueError("This note already exists.") from exc
            return {"id": cur.lastrowid, "content": content}

    def delete_note(self, note_id: int) -> bool:
        with self._connection() as db:
            return db.execute("DELETE FROM memories WHERE id=?", (note_id,)).rowcount > 0
