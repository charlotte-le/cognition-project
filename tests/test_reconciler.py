"""Tests for the reconciler's verdict routing.

The routing in decide_outcome is what decides whether an agent's work is retried,
quarantined, or waited on. It once read the infra flag backwards and quarantined
two correct PRs at attempt 1 of 2, so it is pinned here.
"""

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
