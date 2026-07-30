import sqlite3
import os
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any
import json

from cognition.core import config


class State(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    VERIFYING = "VERIFYING"
    READY = "READY"
    MERGED = "MERGED"
    QUARANTINED = "QUARANTINED"
    FAILED = "FAILED"
    # A human reviewed the PR and closed it without merging. Distinct from
    # QUARANTINED (the harness rejected the work) and from FAILED (the
    # machinery broke): here the pipeline worked and a person said no.
    CLOSED = "CLOSED"


# States a task cannot leave. Nothing reconciles these; they are the record.
TERMINAL_STATES = {State.MERGED, State.QUARANTINED, State.FAILED, State.CLOSED}


def _get_db_path() -> str:
    """Ensure the parent directory exists and return the database path."""
    db_path = config.DB_PATH
    parent_dir = os.path.dirname(db_path)
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)
    return db_path


def _now() -> str:
    """Return current timestamp as ISO string."""
    return datetime.utcnow().isoformat()


def init_db() -> None:
    """Create tables and enable WAL mode. Idempotent."""
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                fp            TEXT PRIMARY KEY,
                class         TEXT NOT NULL,
                issue_number  INTEGER,
                state         TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                payload_json  TEXT,
                current_attempt_id INTEGER,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                -- Why a task reached a terminal state. Without this the reason
                -- for a FAILED or CLOSED task lives only in the logs, and the
                -- status page can only say that something went wrong.
                failure_reason TEXT,
                -- Set once, when the system had to pull a person in mid-flight:
                -- the agent asked a question, or the harness gave up and asked
                -- for a human. A sticky flag rather than a state, because the
                -- task moves on afterwards and the fact that a human was needed
                -- has to survive that move for the autonomy rate to mean
                -- anything. Deliberately NOT set when a reviewer closes a PR:
                -- that is the review gate working as designed, not the machine
                -- failing to get there on its own.
                human_touched INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attempts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                fp             TEXT NOT NULL REFERENCES tasks(fp),
                attempt_no     INTEGER NOT NULL,
                request_id     TEXT NOT NULL,
                session_id     TEXT,
                session_url    TEXT,
                -- The PR this attempt produced, so the status page can link it.
                pr_url         TEXT,
                outcome        TEXT,
                verdict_passed INTEGER,
                gate_failed    TEXT,
                claim_json     TEXT,
                evidence_json  TEXT,
                started_at     TEXT NOT NULL,
                ended_at       TEXT,
                UNIQUE(fp, attempt_no)
            )
        """)

        # CREATE TABLE IF NOT EXISTS leaves databases made before a column was
        # added untouched, so add it here rather than making operators rebuild.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "failure_reason" not in existing:
            conn.execute("ALTER TABLE tasks ADD COLUMN failure_reason TEXT")
        if "human_touched" not in existing:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN human_touched INTEGER NOT NULL DEFAULT 0"
            )

        # Same for attempts.pr_url, which is in the CREATE above but absent from
        # any database created before it was added.
        existing_attempts = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
        if "pr_url" not in existing_attempts:
            conn.execute("ALTER TABLE attempts ADD COLUMN pr_url TEXT")

        conn.commit()


def upsert_task(fp: str, cls: str, payload: Dict[str, Any]) -> bool:
    """Insert a task if it doesn't exist. Returns True only if a row was inserted."""
    db_path = _get_db_path()
    now = _now()
    payload_json = json.dumps(payload) if payload else None
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("""
            INSERT INTO tasks (fp, class, state, attempt_count, payload_json, created_at, updated_at)
            VALUES (?, ?, 'PENDING', 0, ?, ?, ?)
            ON CONFLICT(fp) DO NOTHING
        """, (fp, cls, payload_json, now, now))
        conn.commit()
        return cursor.rowcount == 1


def transition(fp: str, from_state: str, to_state: str, **fields: Any) -> bool:
    """Conditionally update task state. Returns True only if the update succeeded."""
    db_path = _get_db_path()
    now = _now()
    
    set_clauses = ["state = ?", "updated_at = ?"]
    values = [to_state, now]
    
    for key, value in fields.items():
        set_clauses.append(f"{key} = ?")
        if isinstance(value, (dict, list)):
            values.append(json.dumps(value))
        else:
            values.append(value)
    
    values.extend([fp, from_state])
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE tasks SET {', '.join(set_clauses)} WHERE fp = ? AND state = ?",
            values
        )
        conn.commit()
        return cursor.rowcount == 1


def increment_attempt_count(fp: str) -> bool:
    """Increment attempt_count for a task. Returns True only if the update succeeded."""
    db_path = _get_db_path()
    now = _now()
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE tasks SET attempt_count = attempt_count + 1, updated_at = ? WHERE fp = ?",
            [now, fp]
        )
        conn.commit()
        return cursor.rowcount == 1


def mark_human_touched(fp: str) -> bool:
    """Record that this task needed a person mid-flight. Idempotent.

    Not a transition: the task keeps moving after a human unblocks it, and the
    autonomy rate has to remember that it ever stopped. Writing it as a latch
    means calling this twice on the same task cannot double-count.
    """
    db_path = _get_db_path()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE tasks SET human_touched = 1, updated_at = ? "
            "WHERE fp = ? AND human_touched = 0",
            [_now(), fp],
        )
        conn.commit()
        return cursor.rowcount == 1


def get_task(fp: str) -> Optional[Dict[str, Any]]:
    """Get a single task by fingerprint."""
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM tasks WHERE fp = ?", (fp,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


def list_tasks(states: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """List tasks, optionally filtered by state."""
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if states:
            placeholders = ",".join("?" * len(states))
            cursor = conn.execute(f"SELECT * FROM tasks WHERE state IN ({placeholders})", states)
        else:
            cursor = conn.execute("SELECT * FROM tasks")
        return [dict(row) for row in cursor.fetchall()]


def get_current_attempt(fp: str) -> Optional[Dict[str, Any]]:
    """Get the current attempt for a task (based on current_attempt_id)."""
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
            SELECT * FROM attempts 
            WHERE fp = ? AND id = (SELECT current_attempt_id FROM tasks WHERE fp = ?)
        """, (fp, fp))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


def count_active() -> int:
    """Count tasks in active states (RUNNING, BLOCKED, VERIFYING)."""
    db_path = _get_db_path()
    active_states = [State.RUNNING.value, State.BLOCKED.value, State.VERIFYING.value]
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state IN (?, ?, ?)",
            active_states
        )
        return cursor.fetchone()[0]


def start_attempt(fp: str, attempt_no: int, request_id: str) -> int:
    """Insert a new attempt row and return its ID."""
    db_path = _get_db_path()
    now = _now()
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("""
            INSERT INTO attempts (fp, attempt_no, request_id, started_at)
            VALUES (?, ?, ?, ?)
        """, (fp, attempt_no, request_id, now))
        conn.commit()
        return cursor.lastrowid


def finish_attempt(attempt_id: int, **fields: Any) -> None:
    """Update an attempt with completion data."""
    db_path = _get_db_path()
    
    if not fields:
        return
    
    set_clauses = []
    values = []
    
    for key, value in fields.items():
        set_clauses.append(f"{key} = ?")
        if isinstance(value, (dict, list)):
            values.append(json.dumps(value))
        else:
            values.append(value)
    
    values.append(attempt_id)
    
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"UPDATE attempts SET {', '.join(set_clauses)} WHERE id = ?",
            values
        )
        conn.commit()
