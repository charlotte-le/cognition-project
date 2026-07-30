"""Tests for the reconciler's verdict routing and its BLOCKED resume path.

The routing in decide_outcome is what decides whether an agent's work is retried,
quarantined, or waited on. It once read the infra flag backwards and quarantined
two correct PRs at attempt 1 of 2, so it is pinned here.
"""

import asyncio
import importlib
import os
import tempfile

import pytest

from cognition.core import config
from cognition.core.reconciler import (
    OUTCOME_QUARANTINE,
    OUTCOME_READY,
    OUTCOME_RETRY,
    OUTCOME_WAIT,
    decide_outcome,
)
from cognition.verification.verifier import Verdict


def _passed() -> Verdict:
    return Verdict(passed=True, gate=None, reason="All verification gates passed.")


def _rejected(gate: str = "oracle") -> Verdict:
    """A real verdict about the agent's work."""
    return Verdict(passed=False, gate=gate, reason="nope", counts_as_attempt=True)


def _infra(reason: str = "pytest exit 4") -> Verdict:
    """A verdict about the verifier's own environment."""
    return Verdict(passed=False, gate="infra", reason=reason, counts_as_attempt=False)


class TestDecideOutcome:
    """Verdict -> transition."""

    def test_pass_is_ready(self):
        assert decide_outcome(_passed(), attempt_count=0) == OUTCOME_READY

    def test_pass_is_ready_even_at_the_attempt_cap(self):
        """A pass is a pass. Attempts spent getting there do not matter."""
        assert decide_outcome(_passed(), config.MAX_ATTEMPTS) == OUTCOME_READY

    def test_rejection_under_the_cap_retries(self):
        assert decide_outcome(_rejected(), attempt_count=0) == OUTCOME_RETRY
        assert decide_outcome(_rejected(), config.MAX_ATTEMPTS - 1) == OUTCOME_RETRY

    def test_rejection_at_the_cap_quarantines(self):
        assert decide_outcome(_rejected(), config.MAX_ATTEMPTS) == OUTCOME_QUARANTINE
        assert decide_outcome(_rejected(), config.MAX_ATTEMPTS + 1) == OUTCOME_QUARANTINE

    @pytest.mark.parametrize(
        "attempt_count",
        [0, 1, config.MAX_ATTEMPTS, config.MAX_ATTEMPTS + 5],
    )
    def test_infra_always_waits(self, attempt_count):
        """An infra failure says nothing about the agent's work.

        It must never quarantine, at any attempt count. A missing test path or a
        verifier host without the target repo's dependencies is our problem, not
        the agent's, and the task has to stay eligible for re-verification.
        """
        assert decide_outcome(_infra(), attempt_count) == OUTCOME_WAIT

    def test_infra_at_the_cap_does_not_quarantine(self):
        """The exact regression: cap reached AND infra. Work was thrown away here."""
        assert decide_outcome(_infra(), config.MAX_ATTEMPTS) != OUTCOME_QUARANTINE

    def test_every_gate_that_can_return_infra_waits(self):
        """The verifier reports infra from checkout, oracle, and tests alike."""
        for reason in [
            "PR #104 moved during verification",
            "Bandit scanned 0 lines",
            "pytest is not installed in the verifier environment",
            "Test execution timed out",
        ]:
            assert decide_outcome(_infra(reason), attempt_count=0) == OUTCOME_WAIT


@pytest.fixture
def fresh_db():
    """A throwaway ledger, so state transitions are actually written and read."""
    db_path = tempfile.mktemp(suffix=".db")
    os.environ["DB_PATH"] = db_path

    from cognition.core import config as config_mod
    importlib.reload(config_mod)
    from cognition.core import db as db_mod
    importlib.reload(db_mod)

    db_mod.init_db()
    yield db_mod
    os.environ.pop("DB_PATH", None)
    if os.path.exists(db_path):
        os.remove(db_path)


def _blocked_task(db, fp: str) -> dict:
    """A task parked in BLOCKED, the way an agent question leaves it."""
    db.upsert_task(fp, "B608", {"test_id": "B608", "file_path": "superset/x.py"})
    db.transition(fp, db.State.PENDING, db.State.BLOCKED)
    return db.get_task(fp)


class TestBlockedResume:
    """BLOCKED -> RUNNING, the return path for a task an agent asked about.

    This branch referenced a bare `Internal` instead of `devin.Internal`, so every
    tick over a blocked task raised NameError. The per-task `except` in tick()
    swallowed it, which meant a human could answer the agent's question and the
    task would still never resume - it just kept repainting the error banner.
    Nothing exercised this path, which is how it survived.
    """

    def test_answered_question_resumes_the_task(self, fresh_db):
        db = fresh_db
        from cognition.core import reconciler

        fp = "scan:deadbeef"
        task = _blocked_task(db, fp)

        # The human answered; the vendor reports the session working again.
        session = {"session_id": "s1", "status": "running", "status_detail": "working"}

        asyncio.run(reconciler.reconcile_task(fp, task, session, None, None, None))

        assert db.get_task(fp)["state"] == db.State.RUNNING

    def test_still_waiting_stays_blocked(self, fresh_db):
        """No answer yet is not a reason to move. The task holds."""
        db = fresh_db
        from cognition.core import reconciler

        fp = "scan:cafebabe"
        task = _blocked_task(db, fp)

        session = {
            "session_id": "s2",
            "status": "running",
            "status_detail": "waiting_for_user",
        }

        asyncio.run(reconciler.reconcile_task(fp, task, session, None, None, None))

        assert db.get_task(fp)["state"] == db.State.BLOCKED


class _FakeDevin:
    """Records get_session calls so tests can assert what was and wasn't fetched."""

    def __init__(self, sessions: dict = None):
        self.sessions = sessions or {}
        self.fetched = []

    def get_session(self, session_id: str):
        self.fetched.append(session_id)
        return self.sessions.get(session_id)


def _attempt_on(db, fp: str, session_id: str) -> None:
    """Point the task's current attempt at a session, the way create_session does."""
    attempt_id = db.start_attempt(fp, 1, "req-1")
    db.finish_attempt(attempt_id, session_id=session_id)
    db.transition(fp, db.get_task(fp)["state"], db.get_task(fp)["state"],
                  current_attempt_id=attempt_id)


class TestSessionResolution:
    """Which session a task is reconciled against.

    Session titles carry the fingerprint but are not unique to a session: a retry
    reuses the title, and a session outlives the ledger row that created it, so a
    rebuilt ledger leaves suspended corpses still advertising a live fingerprint.
    tick() keeps one entry per fingerprint, so the corpse can win the map.

    This shadowing stranded a real task: scan:1ac121c4 sat in BLOCKED with its PR
    already open, because the map handed BLOCKED a suspended session whose
    pull_requests[] was empty, and BLOCKED - unlike RUNNING - never checked the
    guess against the attempt row.
    """

    def test_blocked_harvests_pr_from_the_attempts_session_not_the_impostor(self, fresh_db):
        db = fresh_db
        from cognition.core import reconciler

        fp = "scan:1ac121c4"
        task = _blocked_task(db, fp)
        _attempt_on(db, fp, "live-session")

        # What the agent actually did: still waiting on the human, PR already up.
        live = {
            "session_id": "live-session",
            "status": "running",
            "status_detail": "waiting_for_user",
            "pull_requests": [{"pr_url": ".../pull/165", "pr_state": "open"}],
            "structured_output": {"claim": "parameterized the query"},
        }
        # A corpse from an earlier generation of the ledger, same title, no PR.
        impostor = {
            "session_id": "stale-session",
            "status": "suspended",
            "status_detail": "inactivity",
            "pull_requests": [],
        }

        # Deliberately the plain call, with no session_by_id: the defect is that
        # BLOCKED trusted the guess, and it has to be fixed for the guess alone.
        asyncio.run(
            reconciler.reconcile_task(
                fp, db.get_task(fp), impostor, None, None,
                _FakeDevin({"live-session": live}),
            )
        )

        assert db.get_task(fp)["state"] == db.State.VERIFYING
        # And the PR and claim got recorded, rather than left pending.
        attempt = db.get_current_attempt(fp)
        assert attempt["pr_url"] == ".../pull/165"
        assert "parameterized the query" in attempt["claim_json"]

    def test_impostor_alone_does_not_strand_a_blocked_task(self, fresh_db):
        """The live session is absent from insights, so it must be fetched by id."""
        db = fresh_db
        from cognition.core import reconciler

        fp = "scan:1ac121c4"
        _blocked_task(db, fp)
        _attempt_on(db, fp, "live-session")

        live = {
            "session_id": "live-session",
            "status": "running",
            "status_detail": "waiting_for_user",
            "pull_requests": [{"pr_url": ".../pull/165", "pr_state": "open"}],
        }
        impostor = {"session_id": "stale-session", "status": "suspended",
                    "status_detail": "inactivity", "pull_requests": []}
        devin_client = _FakeDevin({"live-session": live})

        asyncio.run(
            reconciler.reconcile_task(
                fp, db.get_task(fp), impostor, None, None, devin_client,
                session_by_id={"stale-session": impostor},
            )
        )

        assert devin_client.fetched == ["live-session"]
        assert db.get_task(fp)["state"] == db.State.VERIFYING

    def test_a_matching_guess_costs_no_api_call(self, fresh_db):
        """The common case: insights already gave the right session."""
        db = fresh_db
        from cognition.core import reconciler

        fp = "scan:beefbeef"
        _blocked_task(db, fp)
        _attempt_on(db, fp, "live-session")

        live = {"session_id": "live-session", "status": "running",
                "status_detail": "working", "pull_requests": []}
        devin_client = _FakeDevin()

        asyncio.run(
            reconciler.reconcile_task(
                fp, db.get_task(fp), live, None, None, devin_client,
                session_by_id={"live-session": live},
            )
        )

        assert devin_client.fetched == []
        assert db.get_task(fp)["state"] == db.State.RUNNING

    def test_blocked_with_no_session_at_all_holds(self, fresh_db):
        """Nothing to reconcile against is a reason to wait, not to transition."""
        db = fresh_db
        from cognition.core import reconciler

        fp = "scan:0badf00d"
        _blocked_task(db, fp)
        _attempt_on(db, fp, "live-session")

        asyncio.run(
            reconciler.reconcile_task(
                fp, db.get_task(fp), None, None, None, _FakeDevin(), session_by_id={},
            )
        )

        assert db.get_task(fp)["state"] == db.State.BLOCKED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
