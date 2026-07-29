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
                acus_total    REAL NOT NULL DEFAULT 0,
                payload_json  TEXT,
                current_attempt_id INTEGER,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
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
                outcome        TEXT,
                verdict_passed INTEGER,
                gate_failed    TEXT,
                claim_json     TEXT,
                evidence_json  TEXT,
                acus_consumed  REAL,
                started_at     TEXT NOT NULL,
                ended_at       TEXT,
                UNIQUE(fp, attempt_no)
            )
        """)
        conn.commit()


def upsert_task(fp: str, cls: str, payload: Dict[str, Any]) -> bool:
    """Insert a task if it doesn't exist. Returns True only if a row was inserted."""
    db_path = _get_db_path()
    now = _now()
    payload_json = json.dumps(payload) if payload else None
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("""
            INSERT INTO tasks (fp, class, state, attempt_count, acus_total, payload_json, created_at, updated_at)
            VALUES (?, ?, 'PENDING', 0, 0, ?, ?, ?)
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


def acus_today() -> float:
    """Sum of acus_consumed for attempts that ended today (accurate budget attribution)."""
    db_path = _get_db_path()
    today = datetime.utcnow().date().isoformat()
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT SUM(acus_consumed) FROM attempts WHERE date(ended_at) = ?",
            (today,)
        )
        result = cursor.fetchone()[0]
        return result if result is not None else 0.0
