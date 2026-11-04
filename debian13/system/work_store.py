"""Persistent, operator-review-only Work task ledger for Debian 13.

Task creation DOES NOT start AI workers, execute shell commands, submit purchases,
or issue GitHub requests. SQLite is stored in the mounted private workspace.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_TASKS = 2000
MAX_EVENTS = 5000
PAGE_SIZE = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkStore:
    def __init__(self, workspace: Path):
        self.db_path = workspace / ".debian13-work.sqlite3"
        with self._open() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','cancelled')),
                    execution TEXT NOT NULL DEFAULT 'not_started',
                    requires_approval INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    type TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_latest ON tasks(created_at DESC);
                CREATE TABLE IF NOT EXISTS local_ai_results (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    agent TEXT NOT NULL,
                    model TEXT NOT NULL,
                    content TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_worker_heartbeats (
                    name TEXT PRIMARY KEY,
                    last_seen_at REAL NOT NULL,
                    model TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alex_submissions (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    remote_task_id TEXT NOT NULL,
                    submitted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS original_agent_submissions (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    agent TEXT NOT NULL,
                    remote_task_id TEXT NOT NULL,
                    submitted_at TEXT NOT NULL
                );
            """)
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def _open(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def list_tasks(self) -> list[dict]:
        with self._open() as db:
            return [dict(row) for row in db.execute(
                "SELECT tasks.*, original_agent_submissions.remote_task_id AS originalRemoteTaskId "
                "FROM tasks LEFT JOIN original_agent_submissions "
                "ON original_agent_submissions.task_id = tasks.id "
                "ORDER BY tasks.created_at DESC, tasks.rowid DESC LIMIT ?",
                (PAGE_SIZE,),
            )]

    def list_events(self) -> list[dict]:
        with self._open() as db:
            return [dict(row) for row in db.execute(
                "SELECT task_id,agent,type,at FROM events ORDER BY id DESC LIMIT ?",
                (PAGE_SIZE,),
            )]

    def get_task(self, task_id: str) -> dict | None:
        with self._open() as db:
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def create_task(self, kind: str, agent: str, title: str, description: str) -> dict:
        ident = str(uuid.uuid4())
        created = _now()
        with self._open() as db:
            # Lock before checking the quota so concurrent requests cannot exceed it.
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            if count >= MAX_TASKS:
                raise ValueError("Task quota reached; archive with operator review.")
            db.execute(
                "INSERT INTO tasks(id,kind,agent,title,description,status,execution,"
                "requires_approval,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'queued','not_started',1,?,?)",
                (ident, kind, agent, title, description, created, created),
            )
            db.execute(
                "INSERT INTO events(task_id,agent,type,at) VALUES (?,?,?,?)",
                (ident, agent, "task_queued", created),
            )
            db.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (MAX_EVENTS,),
            )
        return self.get_task(ident)

    def cancel_task(self, task_id: str) -> dict | None:
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT agent,status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                stamp = _now()
                db.execute(
                    "UPDATE tasks SET status='cancelled',requires_approval=1,updated_at=? WHERE id=? AND status='queued'",
                    (stamp, task_id),
                )
                db.execute(
                    "INSERT INTO events(task_id,agent,type,at) VALUES (?,?,?,?)",
                    (task_id, row["agent"], "task_cancelled", stamp),
                )
        return self.get_task(task_id)

    def get_alex_submission(self, task_id: str) -> dict | None:
        with self._open() as db:
            row = db.execute(
                "SELECT remote_task_id, submitted_at FROM alex_submissions WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return {
            "status": "accepted_by_remote_api",
            "remoteTaskId": row["remote_task_id"],
            "submittedAt": row["submitted_at"],
            "agentExecutionVerified": False,
            "pullRequestVerified": False,
        } if row else None

    def record_alex_submission(self, task_id: str, remote_task_id: str) -> dict:
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,agent FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None or row["status"] != "queued" or row["agent"] != "alex":
                raise ValueError("Task no longer eligible for Alex dispatch.")
            if db.execute("SELECT 1 FROM original_agent_submissions WHERE task_id=?",
                          (task_id,)).fetchone():
                raise ValueError("Task already sent to original agent review queue.")
            stamp = _now()
            db.execute(
                "INSERT OR IGNORE INTO alex_submissions(task_id,remote_task_id,submitted_at) "
                "VALUES (?,?,?)", (task_id, remote_task_id, stamp)
            )
            db.execute(
                "INSERT INTO events(task_id,agent,type,at) "
                "SELECT ?,?,?,? WHERE changes() > 0",
                (task_id, "alex", "task_accepted_by_remote_api", stamp)
            )
        return self.get_alex_submission(task_id)

    def get_original_submission(self, task_id: str) -> dict | None:
        with self._open() as db:
            row = db.execute(
                "SELECT agent,remote_task_id,submitted_at FROM original_agent_submissions "
                "WHERE task_id=?", (task_id,)).fetchone()
        return ({"agent": row["agent"], "remoteTaskId": row["remote_task_id"],
                 "submittedAt": row["submitted_at"],
                 "agentExecutionVerified": False, "pullRequestVerified": False,
                 "status": "accepted_by_remote_api"} if row else None)

    def record_original_submission(self, task_id: str, remote_task_id: str) -> dict:
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT agent,status,execution FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if (row is None or row["status"] != "queued"
                    or row["execution"] != "not_started"
                    or self.get_alex_submission(task_id) is not None):
                raise ValueError("Task no longer eligible for original agent review.")
            if db.execute("SELECT 1 FROM original_agent_submissions WHERE task_id=?",
                          (task_id,)).fetchone():
                raise ValueError("Original agent handoff already recorded.")
            stamp = _now()
            db.execute(
                "INSERT INTO original_agent_submissions"
                "(task_id,agent,remote_task_id,submitted_at) VALUES (?,?,?,?)",
                (task_id, row["agent"], remote_task_id, stamp))
            db.execute(
                "INSERT INTO events(task_id,agent,type,at) VALUES (?,?,?,?)",
                (task_id, row["agent"], "original_review_accepted_by_api", stamp))
        return self.get_original_submission(task_id)

    # Local Ollama workers operate on explicitly approved, safe analysis jobs.
    # These methods NEVER run shell commands or permit remote network actions.
    def approve_local_analysis(self, task_id: str) -> dict | None:
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id, agent, status, execution, requires_approval FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "queued" or row["execution"] not in (
                    "not_started", "interrupted_local_analysis", "failed_local_analysis"
            ):
                raise ValueError("This task is not eligible for local analysis.")
            if db.execute("SELECT 1 FROM alex_submissions WHERE task_id=?", (task_id,)).fetchone():
                raise ValueError("This task was already submitted to the remote Alex API.")
            if db.execute("SELECT 1 FROM original_agent_submissions WHERE task_id=?", (task_id,)).fetchone():
                raise ValueError("This task was already sent to an original AI Studio agent.")
            stamp = _now()
            db.execute(
                "UPDATE tasks SET execution='approved_local_analysis', "
                "requires_approval=0,updated_at=? WHERE id=?", (stamp, task_id)
            )
            db.execute(
                "INSERT INTO events(task_id,agent,type,at) VALUES (?,?,?,?)",
                (task_id, row["agent"], "local_analysis_approved", stamp)
            )
        return self.get_task(task_id)

    def claim_local_analysis(self) -> dict | None:
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM tasks WHERE status='queued' AND requires_approval=0 "
                "AND execution='approved_local_analysis' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            stamp = _now()
            db.execute(
                "UPDATE tasks SET execution='running_local_analysis',updated_at=? "
                "WHERE id=? AND execution='approved_local_analysis'",
                (stamp, row["id"])
            )
            db.execute(
                "INSERT INTO events(task_id,agent,type,at) VALUES (?,?,?,?)",
                (row["id"], row["agent"], "local_analysis_started", stamp)
            )
        return self.get_task(row["id"])

    def finish_local_analysis(self, task_id: str, *, model: str,
                              content: str | None = None, failed: bool = False) -> bool:
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT agent,execution,status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["execution"] != "running_local_analysis" or row["status"] != "queued":
                return False
            stamp = _now()
            execution = "failed_local_analysis" if failed else "completed_local_analysis"
            if not failed and content:
                db.execute(
                    "INSERT INTO local_ai_results(task_id,agent,model,content,finished_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                    "model=excluded.model,content=excluded.content,finished_at=excluded.finished_at",
                    (task_id, row["agent"], model[:120], content[:16000], stamp)
                )
            db.execute(
                "UPDATE tasks SET execution=?,requires_approval=1,updated_at=? WHERE id=?",
                (execution, stamp, task_id)
            )
            db.execute(
                "INSERT INTO events(task_id,agent,type,at) VALUES (?,?,?,?)",
                (task_id, row["agent"],
                 "local_analysis_failed" if failed else "local_analysis_completed", stamp)
            )
        return True

    def result_for_task(self, task_id: str) -> dict | None:
        with self._open() as db:
            row = db.execute(
                "SELECT agent,model,content,finished_at FROM local_ai_results WHERE task_id=?",
                (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def recover_interrupted_analysis(self) -> int:
        """Fail closed after a process crash; never silently replay work."""
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,agent FROM tasks WHERE execution='running_local_analysis'"
            ).fetchall()
            stamp = _now()
            for row in rows:
                db.execute(
                    "UPDATE tasks SET execution='interrupted_local_analysis',"
                    "requires_approval=1,updated_at=? WHERE id=?",
                    (stamp, row["id"])
                )
                db.execute(
                    "INSERT INTO events(task_id,agent,type,at) VALUES (?,?,?,?)",
                    (row["id"], row["agent"], "local_analysis_interrupted", stamp)
                )
        return len(rows)

    def heartbeat_local_worker(self, model: str) -> None:
        with self._open() as db:
            db.execute(
                "INSERT INTO local_worker_heartbeats(name,last_seen_at,model) "
                "VALUES('ollama',?,?) ON CONFLICT(name) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at,model=excluded.model",
                (time.time(), model[:120])
            )

    def local_worker_status(self) -> dict:
        with self._open() as db:
            row = db.execute(
                "SELECT last_seen_at,model FROM local_worker_heartbeats WHERE name='ollama'"
            ).fetchone()
            counts = db.execute(
                "SELECT execution,COUNT(*) AS qty FROM tasks "
                "WHERE execution LIKE '%local_analysis' GROUP BY execution"
            ).fetchall()
        age = round(max(0, time.time() - row["last_seen_at"])) if row else None
        return {
            "type": "local_ollama_queue",
            "workerHeartbeatVerified": age is not None and age <= 120,
            "heartbeatAgeSeconds": age,
            "modelConfigured": row["model"] if row else None,
            "executionVerified": False,  # Worker heartbeat proves a process, not inference.
            "queuedByExecution": {item["execution"]: item["qty"] for item in counts},
            "originalAgentsConnected": False,
            "chatGPTWorkRequired": False,
            "notes": "Local analysis only: no shell, code modification, git push or root.",
        }
