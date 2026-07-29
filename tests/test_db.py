import os
import tempfile
import sqlite3
import pytest
import importlib

# Set required environment variables for testing
os.environ["DEVIN_API_KEY"] = "test-key"
os.environ["DEVIN_ORG_ID"] = "test-org"
os.environ["GITHUB_TOKEN"] = "test-token"


@pytest.fixture(autouse=True)
def fresh_db():
    """Create a fresh database for each test."""
    db_path = tempfile.mktemp(suffix=".db")
    os.environ["DB_PATH"] = db_path
    
    # Reload config to pick up the new DB_PATH
    from cognition.core import config
    importlib.reload(config)
    
    # Reload db to pick up the new config
    from cognition.core import db
    importlib.reload(db)
    
    db.init_db()
    yield
    # Clean up the test database
    if os.path.exists(db_path):
        os.remove(db_path)


def test_upsert_task_twice_inserts_once():
    """Test that upsert_task twice inserts only once."""
    from cognition.core import db
    fp = "test-file.py"
    payload = {"fingerprint": fp, "severity": "high"}
    
    # First insert should succeed
    result1 = db.upsert_task(fp, "BanditFinding", payload)
    assert result1 is True, "First upsert should return True (inserted)"
    
    # Second insert should be a no-op
    result2 = db.upsert_task(fp, "BanditFinding", payload)
    assert result2 is False, "Second upsert should return False (not inserted)"
    
    # Verify only one row exists
    task = db.get_task(fp)
    assert task is not None, "Task should exist"
    assert task["state"] == db.State.PENDING, "Task should be in PENDING state"
    assert task["attempt_count"] == 0, "Task should have 0 attempts"


def test_transition_from_wrong_state_returns_false():
    """Test that transition from the wrong state returns False and changes nothing."""
    from cognition.core import db
    fp = "test-wrong-state.py"
    db.upsert_task(fp, "BanditFinding", {"fingerprint": fp})
    
    # Try to transition from RUNNING when task is PENDING
    result = db.transition(fp, db.State.RUNNING, db.State.BLOCKED)
    assert result is False, "Transition from wrong state should return False"
    
    # Verify state is still PENDING
    task = db.get_task(fp)
    assert task["state"] == db.State.PENDING, "State should still be PENDING"


def test_transition_from_right_state_returns_true():
    """Test that transition from the right state returns True."""
    from cognition.core import db
    fp = "test-right-state.py"
    db.upsert_task(fp, "BanditFinding", {"fingerprint": fp})
    
    # Transition from PENDING to RUNNING (correct state)
    result = db.transition(fp, db.State.PENDING, db.State.RUNNING)
    assert result is True, "Transition from correct state should return True"
    
    # Verify state is now RUNNING
    task = db.get_task(fp)
    assert task["state"] == db.State.RUNNING, "State should be RUNNING"


def test_unique_fp_attempt_no_rejects_duplicate():
    """Test that UNIQUE(fp, attempt_no) rejects duplicate attempts."""
    from cognition.core import db
    fp = "test-unique-attempt.py"
    db.upsert_task(fp, "BanditFinding", {"fingerprint": fp})
    db.transition(fp, db.State.PENDING, db.State.RUNNING)
    
    # Start first attempt
    attempt1_id = db.start_attempt(fp, 1, "req-001")
    assert attempt1_id > 0, "First attempt should be created"
    
    # Try to start duplicate attempt (same fp, same attempt_no)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        db.start_attempt(fp, 1, "req-002")
    
    # Verify we can still start a different attempt number
    attempt2_id = db.start_attempt(fp, 2, "req-003")
    assert attempt2_id > 0, "Second attempt with different number should succeed"


def test_state_transitions_chain():
    """Test a complete chain of state transitions."""
    from cognition.core import db
    fp = "test-chain.py"
    db.upsert_task(fp, "BanditFinding", {"fingerprint": fp})
    
    # Chain: PENDING -> RUNNING -> VERIFYING -> READY
    assert db.transition(fp, db.State.PENDING, db.State.RUNNING) is True
    assert db.transition(fp, db.State.RUNNING, db.State.VERIFYING) is True
    assert db.transition(fp, db.State.VERIFYING, db.State.READY) is True
    
    task = db.get_task(fp)
    assert task["state"] == db.State.READY


def test_transition_with_additional_fields():
    """Test that transition can update additional fields."""
    from cognition.core import db
    fp = "test-fields.py"
    db.upsert_task(fp, "BanditFinding", {"fingerprint": fp})
    
    # Transition with additional fields
    result = db.transition(
        fp, 
        db.State.PENDING, 
        db.State.RUNNING, 
        attempt_count=1,
        acus_total=5.5
    )
    assert result is True
    
    task = db.get_task(fp)
    assert task["attempt_count"] == 1
    assert task["acus_total"] == 5.5


def test_start_and_finish_attempt():
    """Test starting and finishing an attempt."""
    from cognition.core import db
    fp = "test-attempt-lifecycle.py"
    db.upsert_task(fp, "BanditFinding", {"fingerprint": fp})
    db.transition(fp, db.State.PENDING, db.State.RUNNING)
    
    # Start attempt
    attempt_id = db.start_attempt(fp, 1, "req-123")
    assert attempt_id > 0
    
    # Finish attempt
    db.finish_attempt(
        attempt_id,
        session_id="session-abc",
        session_url="https://devin.ai/s/abc",
        outcome="success",
        verdict_passed=1,
        claim_json={"fix_applied": True},
        evidence_json={"verdict": "accept"}
    )
    
    # Verify attempt was updated (by checking the DB directly)
    db_path = os.environ["DB_PATH"]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
        attempt = dict(cursor.fetchone())
        assert attempt["session_id"] == "session-abc"
        assert attempt["outcome"] == "success"
        assert attempt["verdict_passed"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
