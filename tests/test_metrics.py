"""Tests for the outcome metrics on the status page.

Autonomy, first-pass yield, hours returned and discovery→review time are the
numbers an engineering leader reads to decide whether this system is worth
running, so what each one counts is pinned here. The interesting cases are all
about what is deliberately excluded: work still in flight, a PR a reviewer
declined, a task that needed a person halfway through, and the two queues on
either end of the cycle clock that belong to the backlog and to the reviewer.
"""

import importlib
import os
import sqlite3
import tempfile

import pytest

# Set required environment variables for testing
os.environ["DEVIN_API_KEY"] = "test-key"
os.environ["DEVIN_ORG_ID"] = "test-org"
os.environ["GITHUB_TOKEN"] = "test-token"


@pytest.fixture(autouse=True)
def fresh_db():
    """Create a fresh database for each test."""
    db_path = tempfile.mktemp(suffix=".db")
    os.environ["DB_PATH"] = db_path

    from cognition.core import config
    importlib.reload(config)

    from cognition.core import db
    importlib.reload(db)

    from cognition.web import web
    importlib.reload(web)

    db.init_db()
    yield
    if os.path.exists(db_path):
        os.remove(db_path)


def _task(fp: str, state: str, human_touched: int = 0) -> None:
    """Drop a task straight into a state, skipping the reconciler."""
    from cognition.core import db

    db.upsert_task(fp, "B608", {"test_id": "B608"})
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute(
            "UPDATE tasks SET state = ?, human_touched = ? WHERE fp = ?",
            (state, human_touched, fp),
        )
        conn.commit()


def _attempt(fp: str, attempt_no: int, verdict_passed: int) -> None:
    """Record an attempt that the verifier returned a verdict on."""
    from cognition.core import db

    attempt_id = db.start_attempt(fp, attempt_no, f"req-{fp}-{attempt_no}")
    db.finish_attempt(attempt_id, verdict_passed=verdict_passed)


def _metrics() -> dict:
    from cognition.web import web

    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.row_factory = sqlite3.Row
        return web._outcome_metrics(conn)


# --- autonomy -------------------------------------------------------------


def test_autonomy_counts_delivered_work_that_needed_nobody():
    _task("scan:a", "READY")
    _task("scan:b", "MERGED")

    autonomy = _metrics()["autonomy"]
    assert autonomy["autonomous"] == 2
    assert autonomy["resolved"] == 2
    assert autonomy["rate"] == 1.0


def test_autonomy_excludes_work_still_in_flight():
    """A task that has not finished yet is not evidence either way."""
    _task("scan:a", "READY")
    _task("scan:b", "PENDING")
    _task("scan:c", "RUNNING")
    _task("scan:d", "VERIFYING")

    autonomy = _metrics()["autonomy"]
    assert autonomy["resolved"] == 1
    assert autonomy["rate"] == 1.0


def test_autonomy_counts_blocked_against_the_rate():
    """Stopping to ask a question is the whole thing this metric measures."""
    _task("scan:a", "READY")
    _task("scan:b", "BLOCKED", human_touched=1)

    autonomy = _metrics()["autonomy"]
    assert autonomy["autonomous"] == 1
    assert autonomy["resolved"] == 2
    assert autonomy["interrupted"] == 1
    assert autonomy["rate"] == 0.5


def test_autonomy_does_not_credit_a_task_that_needed_a_human_on_the_way():
    """READY is not enough - it has to have got there unattended."""
    _task("scan:a", "READY", human_touched=1)

    autonomy = _metrics()["autonomy"]
    assert autonomy["autonomous"] == 0
    assert autonomy["resolved"] == 1
    assert autonomy["rate"] == 0.0


def test_autonomy_credits_a_pr_a_reviewer_declined():
    """CLOSED means the review gate worked, not that the machine needed help.

    Counting it against autonomy would let a picky reviewer make a system that
    delivers perfectly well look like one that cannot run on its own.
    """
    _task("scan:a", "CLOSED")

    autonomy = _metrics()["autonomy"]
    assert autonomy["autonomous"] == 1
    assert autonomy["rate"] == 1.0


def test_autonomy_is_none_when_nothing_has_resolved():
    _task("scan:a", "PENDING")
    assert _metrics()["autonomy"]["rate"] is None


# --- first-pass yield -----------------------------------------------------


def test_first_pass_yield_counts_attempt_one_passes():
    _task("scan:a", "READY")
    _attempt("scan:a", 1, verdict_passed=1)

    first_pass = _metrics()["first_pass"]
    assert first_pass["first_pass"] == 1
    assert first_pass["judged"] == 1
    assert first_pass["rate"] == 1.0


def test_first_pass_yield_excludes_a_task_repaired_on_attempt_two():
    """The repair loop worked, but this is not a first-pass success."""
    _task("scan:a", "READY")
    _attempt("scan:a", 1, verdict_passed=0)
    _attempt("scan:a", 2, verdict_passed=1)

    first_pass = _metrics()["first_pass"]
    assert first_pass["first_pass"] == 0
    assert first_pass["judged"] == 1
    assert first_pass["rate"] == 0.0


def test_first_pass_yield_counts_tasks_not_attempts():
    """Two rejections on one task is one task that did not land, not two."""
    _task("scan:a", "QUARANTINED")
    _attempt("scan:a", 1, verdict_passed=0)
    _attempt("scan:a", 2, verdict_passed=0)
    _task("scan:b", "READY")
    _attempt("scan:b", 1, verdict_passed=1)

    first_pass = _metrics()["first_pass"]
    assert first_pass["judged"] == 2
    assert first_pass["first_pass"] == 1
    assert first_pass["rate"] == 0.5


def test_first_pass_yield_ignores_attempts_with_no_verdict():
    """A session still running has made no claim the verifier has ruled on."""
    from cognition.core import db

    _task("scan:a", "RUNNING")
    db.start_attempt("scan:a", 1, "req-a-1")

    assert _metrics()["first_pass"]["rate"] is None


# --- hours returned -------------------------------------------------------


def test_hours_returned_multiplies_remediated_by_the_assumption():
    from cognition.core import config

    _task("scan:a", "READY")
    _task("scan:b", "MERGED")

    returned = _metrics()["hours_returned"]
    assert returned["remediated"] == 2
    assert returned["hours"] == pytest.approx(2 * config.HUMAN_FIX_MINUTES / 60)
    assert returned["assumed_minutes_per_fix"] == config.HUMAN_FIX_MINUTES


def test_hours_returned_excludes_a_declined_pr():
    """A closed PR buys back nothing - the finding is still open."""
    _task("scan:a", "MERGED")
    _task("scan:b", "CLOSED")

    assert _metrics()["hours_returned"]["remediated"] == 1


def test_hours_returned_excludes_quarantined_and_failed_work():
    _task("scan:a", "QUARANTINED")
    _task("scan:b", "FAILED")

    returned = _metrics()["hours_returned"]
    assert returned["remediated"] == 0
    assert returned["hours"] == 0


# --- the human_touched latch ----------------------------------------------


def test_mark_human_touched_is_a_latch():
    """Calling it twice must not let one interruption count as two."""
    from cognition.core import db

    _task("scan:a", "RUNNING")
    assert db.mark_human_touched("scan:a") is True
    assert db.mark_human_touched("scan:a") is False
    assert db.get_task("scan:a")["human_touched"] == 1


def test_mark_human_touched_survives_the_task_moving_on():
    """The task carries on to READY; the fact that it stalled must not vanish."""
    from cognition.core import db

    _task("scan:a", "BLOCKED")
    db.mark_human_touched("scan:a")
    db.transition("scan:a", db.State.BLOCKED, db.State.READY)

    assert db.get_task("scan:a")["human_touched"] == 1
    assert _metrics()["autonomy"]["autonomous"] == 0


# --- discovery to review --------------------------------------------------


def _created_at(fp: str, when: str) -> None:
    """Backdate the scanner row, which is not where this clock starts."""
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute("UPDATE tasks SET created_at = ? WHERE fp = ?", (when, fp))
        conn.commit()


def _timed_attempt(
    fp: str,
    attempt_no: int,
    started_at: str,
    ended_at: str = None,
    verdict_passed: int = None,
) -> None:
    """Record an attempt with its clock set by hand."""
    from cognition.core import db

    attempt_id = db.start_attempt(fp, attempt_no, f"req-{fp}-{attempt_no}")
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute(
            "UPDATE attempts SET started_at = ?, ended_at = ?, verdict_passed = ? "
            "WHERE id = ?",
            (started_at, ended_at, verdict_passed, attempt_id),
        )
        conn.commit()


def _cycle_time():
    from cognition.web import web

    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.row_factory = sqlite3.Row
        return web._avg_discovery_to_review_minutes(conn)


def test_clock_starts_when_the_session_starts_not_when_the_scanner_found_it():
    """The wait in the queue is backlog depth, and is not this system's doing."""
    _task("scan:a", "READY")
    _created_at("scan:a", "2026-07-29T08:00:00")
    _timed_attempt(
        "scan:a", 1, "2026-07-29T09:00:00", "2026-07-29T09:30:00", verdict_passed=1
    )

    assert _cycle_time() == pytest.approx(30, abs=0.1)


def test_clock_stops_when_the_pr_is_ready_for_a_human():
    """Not when a reviewer eventually merges - that queue belongs to them."""
    _task("scan:a", "MERGED")
    _timed_attempt(
        "scan:a", 1, "2026-07-29T09:00:00", "2026-07-29T09:45:00", verdict_passed=1
    )
    # A merge landing hours later must not move the number.
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE fp = ?",
            ("2026-07-29T17:00:00", "scan:a"),
        )
        conn.commit()

    assert _cycle_time() == pytest.approx(45, abs=0.1)


def test_a_task_that_needed_the_repair_loop_is_timed_from_its_first_session():
    """Timing only the winning attempt would make failing twice look free."""
    _task("scan:a", "READY")
    _timed_attempt(
        "scan:a", 1, "2026-07-29T09:00:00", "2026-07-29T09:20:00", verdict_passed=0
    )
    _timed_attempt(
        "scan:a", 2, "2026-07-29T09:40:00", "2026-07-29T10:00:00", verdict_passed=1
    )

    assert _cycle_time() == pytest.approx(60, abs=0.1)


def test_work_that_has_not_reached_review_is_not_averaged_in():
    """A session still running has no cycle to report, not a fast one."""
    _task("scan:a", "READY")
    _timed_attempt(
        "scan:a", 1, "2026-07-29T09:00:00", "2026-07-29T09:30:00", verdict_passed=1
    )
    _task("scan:b", "RUNNING")
    _timed_attempt("scan:b", 1, "2026-07-29T09:00:00")
    _task("scan:c", "FAILED")
    _timed_attempt(
        "scan:c", 1, "2026-07-29T09:00:00", "2026-07-29T12:00:00", verdict_passed=0
    )

    assert _cycle_time() == pytest.approx(30, abs=0.1)


def test_cycle_time_is_none_when_nothing_has_reached_review():
    _task("scan:a", "RUNNING")
    _timed_attempt("scan:a", 1, "2026-07-29T09:00:00")

    assert _cycle_time() is None


# --- rendering ------------------------------------------------------------


def test_percent_renders_an_em_dash_rather_than_zero_when_there_is_no_data():
    """0% and "nothing has happened yet" are very different claims."""
    from cognition.web import web

    assert web._pct(None) == "—"
    assert web._pct(0.0) == "0%"
    assert web._pct(0.7857) == "79%"


def test_hours_drops_the_decimal_once_it_stops_carrying_information():
    from cognition.web import web

    assert web._hours(0) == "0h"
    assert web._hours(7.5) == "7.5h"
    assert web._hours(23.4) == "23h"
