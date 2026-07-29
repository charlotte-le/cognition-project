import os
import sys
import json
from datetime import datetime, timedelta

# Set required environment variables for demo purposes (only if not set)
if not os.environ.get("DEVIN_API_KEY") or os.environ.get("DEVIN_API_KEY") == "":
    os.environ["DEVIN_API_KEY"] = "demo-key"
if not os.environ.get("DEVIN_ORG_ID") or os.environ.get("DEVIN_ORG_ID") == "":
    os.environ["DEVIN_ORG_ID"] = "demo-org"
if not os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") == "":
    os.environ["GITHUB_TOKEN"] = "demo-token"

import config
from db import init_db, upsert_task, transition, start_attempt, finish_attempt, State


def seed_demo():
    """Populate the database with realistic demo data."""
    init_db()
    
    now = datetime.utcnow()
    
    # Helper to create realistic payloads
    def make_payload(fp: str, severity: str = "medium") -> dict:
        return {
            "test_id": "B608",
            "file_path": f"superset/{fp}",
            "line_number": 42,
            "code": f"query = 'SELECT * FROM table WHERE id = ' + user_input",
            "issue_text": f"Potential SQL injection in {fp}",
            "severity": severity,
        }
    
    # PENDING tasks (only 1 to stay under concurrency limit)
    fp = f"scan:{0:08x}"
    upsert_task(fp, "B608", make_payload(fp))
    
    # Create an attempt for the PENDING task
    attempt_id = start_attempt(fp, 1, "req-pending")
    transition(fp, State.PENDING, State.PENDING, issue_number=100, current_attempt_id=attempt_id)
    
    # RUNNING task (1 active)
    fp_running = "scan:00000001"
    upsert_task(fp_running, "B608", make_payload(fp_running, "high"))
    attempt_id_running = start_attempt(fp_running, 1, "req-running")
    transition(fp_running, State.PENDING, State.RUNNING, attempt_count=1, issue_number=101, current_attempt_id=attempt_id_running)
    
    # BLOCKED task (would be 2nd active - skip to keep limit)
    # fp_blocked = "scan:00000002"
    # upsert_task(fp_blocked, "B608", make_payload(fp_blocked))
    # transition(fp_blocked, State.PENDING, State.BLOCKED, attempt_count=2)
    
    # VERIFYING task (would be 3rd active - skip to keep limit)
    # fp_verifying = "scan:00000003"
    # upsert_task(fp_verifying, "B608", make_payload(fp_verifying, "high"))
    # transition(fp_verifying, State.PENDING, State.RUNNING, attempt_count=1)
    # transition(fp_verifying, State.RUNNING, State.VERIFYING, attempt_count=1)
    
    # READY task (not active)
    fp_ready = "scan:00000004"
    upsert_task(fp_ready, "B608", make_payload(fp_ready))
    attempt_id_ready = start_attempt(fp_ready, 1, "req-ready")
    transition(fp_ready, State.PENDING, State.RUNNING, attempt_count=1, issue_number=102, current_attempt_id=attempt_id_ready)
    transition(fp_ready, State.RUNNING, State.VERIFYING, attempt_count=1)
    transition(fp_ready, State.VERIFYING, State.READY, attempt_count=1, acus_total=8.5)
    finish_attempt(attempt_id_ready, outcome="completed", claim_json=json.dumps({"summary": "fixed"}), verdict_passed=1, acus_consumed=8.5, ended_at=(now - timedelta(minutes=30)).isoformat())
    
    # MERGED task (not active)
    fp_merged = "scan:00000005"
    upsert_task(fp_merged, "B608", make_payload(fp_merged))
    attempt_id_merged = start_attempt(fp_merged, 1, "req-merged")
    transition(fp_merged, State.PENDING, State.RUNNING, attempt_count=1, issue_number=103, current_attempt_id=attempt_id_merged)
    transition(fp_merged, State.RUNNING, State.VERIFYING, attempt_count=1)
    transition(fp_merged, State.VERIFYING, State.READY, attempt_count=1, acus_total=12.0)
    transition(fp_merged, State.READY, State.MERGED, attempt_count=1, acus_total=12.0)
    finish_attempt(attempt_id_merged, outcome="completed", claim_json=json.dumps({"summary": "fixed"}), verdict_passed=1, acus_consumed=12.0, ended_at=(now - timedelta(hours=2)).isoformat())
    
    # QUARANTINED task (not active)
    fp_quarantined = "scan:00000006"
    upsert_task(fp_quarantined, "B608", make_payload(fp_quarantined, "critical"))
    attempt_id_quarantined = start_attempt(fp_quarantined, 1, "req-quarantined")
    transition(fp_quarantined, State.PENDING, State.RUNNING, attempt_count=1, issue_number=104, current_attempt_id=attempt_id_quarantined)
    transition(fp_quarantined, State.RUNNING, State.QUARANTINED, attempt_count=1)
    finish_attempt(attempt_id_quarantined, outcome="completed", claim_json=json.dumps({"summary": "fixed"}), verdict_passed=0, gate_failed="policy", acus_consumed=9.8, ended_at=(now - timedelta(hours=1)).isoformat())
    
    # FAILED task (not active)
    fp_failed = "scan:00000007"
    upsert_task(fp_failed, "B608", make_payload(fp_failed))
    attempt_id_failed = start_attempt(fp_failed, 1, "req-failed")
    transition(fp_failed, State.PENDING, State.RUNNING, attempt_count=1, issue_number=105, current_attempt_id=attempt_id_failed)
    transition(fp_failed, State.RUNNING, State.FAILED, attempt_count=2)
    finish_attempt(attempt_id_failed, outcome="failed", ended_at=(now - timedelta(minutes=15)).isoformat())
    
    print(f"Demo data seeded successfully!")
    print(f"Database: {config.DB_PATH}")
    print(f"Total tasks: 5 states represented")


if __name__ == "__main__":
    seed_demo()
