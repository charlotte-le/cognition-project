"""Tests for verifier.py using fixture diffs.

These tests run offline with hand-crafted diffs to verify all gates work correctly.
"""

import json
import subprocess

import pytest
from pathlib import Path

from cognition.core import config
from cognition.verification import verifier
from cognition.verification.verifier import VerifyContext, verify

# Use project root directory for tests that need a valid path
TEST_REPO_PATH = Path(__file__).parent


class TestGate1Join:
    """Tests for Gate 1: PR body and branch validation."""
    
    def test_missing_fixes_in_pr_body(self):
        """PR body missing 'Fixes #' fails gate 1."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="This PR fixes a security issue.",  # Missing "Fixes #42"
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ new line",
            repo_path=Path("/tmp/test"),
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "join"
        assert "Fixes #42" in verdict.reason
        assert verdict.counts_as_attempt is True
    
    def test_missing_footer_marker(self):
        """PR body missing footer marker fails gate 1."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42",  # Missing footer
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ new line",
            repo_path=Path("/tmp/test"),
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "join"
        assert "cognition-project:fp=abc123" in verdict.reason
        assert verdict.counts_as_attempt is True
    
    def test_wrong_branch_name(self):
        """Branch name not matching cognition-project/<fp> fails gate 1."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="wrong-branch-name",  # Should be cognition-project/abc123
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ new line",
            repo_path=Path("/tmp/test"),
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "join"
        assert "cognition-project/abc123" in verdict.reason
        assert verdict.counts_as_attempt is True
    
    def test_gate1_pass(self):
        """Correct PR body and branch name passes gate 1."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ new line",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        # Should not fail at gate 1 (will fail later at gate 3 due to no actual repo)
        if verdict.gate == "join":
            pytest.fail("Gate 1 should have passed")
        # Infra failures (like missing bandit) don't count as attempts
        if verdict.gate == "infra":
            assert verdict.counts_as_attempt is False
        else:
            assert verdict.counts_as_attempt is True


class TestGate2Policy:
    """Tests for Gate 2: diff and file list validation."""
    
    def test_added_nosec_fails(self):
        """Diff adding # nosec fails gate 2 with exact reason string."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ risky_code()  # nosec",
            repo_path=Path("/tmp/test"),
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "policy"
        assert verdict.reason == "You added a suppression comment. That is not a fix. Fix the underlying finding."
        assert verdict.counts_as_attempt is True
    
    def test_added_noqa_fails(self):
        """Diff adding # noqa fails gate 2 with exact reason string."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ risky_code()  # noqa",
            repo_path=Path("/tmp/test"),
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "policy"
        assert verdict.reason == "You added a suppression comment. That is not a fix. Fix the underlying finding."
        assert verdict.counts_as_attempt is True
    
    def test_added_type_ignore_fails(self):
        """Diff adding # type: ignore fails gate 2 with exact reason string."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ risky_code()  # type: ignore",
            repo_path=Path("/tmp/test"),
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "policy"
        assert verdict.reason == "You added a suppression comment. That is not a fix. Fix the underlying finding."
        assert verdict.counts_as_attempt is True
    
    def test_removed_noqa_passes(self):
        """Diff that removes an existing # noqa passes gate 2."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n- risky_code()  # noqa\n+ risky_code()",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        # Should not fail at gate 2 (will fail later at gate 3)
        if verdict.gate == "policy":
            pytest.fail("Gate 2 should have passed (removed suppressions are allowed)")
        # Infra failures (like missing bandit) don't count as attempts
        if verdict.gate == "infra":
            assert verdict.counts_as_attempt is False
        else:
            assert verdict.counts_as_attempt is True
    
    def test_large_diff_fails(self):
        """A 200-line diff fails gate 2."""
        # Create a diff with 200 added lines
        added_lines = ["+ line{}".format(i) for i in range(200)]
        large_diff = "diff --git a/superset/file.py b/superset/file.py\n" + "\n".join(added_lines)
        
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff=large_diff,
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "policy"
        assert "too large" in verdict.reason.lower()
        assert "200" in verdict.reason
        assert verdict.counts_as_attempt is True
    
    def test_requirements_txt_fails(self):
        """Diff touching requirements.txt fails gate 2."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["requirements.txt"],
            diff="diff --git a/requirements.txt b/requirements.txt\n+ new-package==1.0.0",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "policy"
        assert "requirements.txt" in verdict.reason.lower()
        assert "dependency" in verdict.reason.lower()
        assert verdict.counts_as_attempt is True
    
    def test_setup_py_fails(self):
        """Diff touching setup.py fails gate 2."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["setup.py"],
            diff="diff --git a/setup.py b/setup.py\n+ new line",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "policy"
        assert "setup.py" in verdict.reason.lower()
        assert "dependency" in verdict.reason.lower()
        assert verdict.counts_as_attempt is True
    
    def test_pyproject_toml_fails(self):
        """Diff touching pyproject.toml fails gate 2."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["pyproject.toml"],
            diff="diff --git a/pyproject.toml b/pyproject.toml\n+ new line",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "policy"
        assert "pyproject.toml" in verdict.reason.lower()
        assert "dependency" in verdict.reason.lower()
        assert verdict.counts_as_attempt is True
    
    def test_package_json_fails(self):
        """Diff touching package.json fails gate 2."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["package.json"],
            diff="diff --git a/package.json b/package.json\n+ new line",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "policy"
        assert "package.json" in verdict.reason.lower()
        assert "dependency" in verdict.reason.lower()
        assert verdict.counts_as_attempt is True
    
    def test_poetry_lock_fails(self):
        """Diff touching poetry.lock fails gate 2."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["poetry.lock"],
            diff="diff --git a/poetry.lock b/poetry.lock\n+ new line",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "policy"
        assert "poetry.lock" in verdict.reason.lower()
        assert "dependency" in verdict.reason.lower()
        assert verdict.counts_as_attempt is True
    
    def test_wrong_path_prefix_fails(self):
        """File not in PATH_ALLOWLIST fails gate 2."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["other/file.py"],  # Not in superset/ or tests/
            diff="diff --git a/other/file.py b/other/file.py\n+ new line",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        assert not verdict.passed
        assert verdict.gate == "policy"
        assert "other/file.py" in verdict.reason
        assert "allowed paths" in verdict.reason.lower()
        assert verdict.counts_as_attempt is True
    
    def test_clean_small_diff_passes_gate2(self):
        """A clean small diff passes gate 2."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ def safe_function():\n+     return 42",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        # Should not fail at gate 2 (will fail later at gate 3)
        if verdict.gate == "policy":
            pytest.fail("Gate 2 should have passed for clean small diff")
        # Infra failures (like missing bandit) don't count as attempts
        if verdict.gate == "infra":
            assert verdict.counts_as_attempt is False
        else:
            assert verdict.counts_as_attempt is True


class TestGate3Oracle:
    """Tests for Gate 3: Bandit execution."""
    
    def test_clean_diff_reaches_gate3(self):
        """A clean small diff reaches gate 3 (will fail due to no actual repo)."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ def safe_function():\n+     return 42",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        # Should pass gates 1 and 2, fail at gate 3 (no actual repo)
        assert verdict.gate in ["oracle", "infra", "tests"]  # oracle or infra (timeout) or tests
        assert verdict.gate not in ["join", "policy"]
        # Infra failures (like missing bandit) don't count as attempts
        if verdict.gate == "infra":
            assert verdict.counts_as_attempt is False
        else:
            assert verdict.counts_as_attempt is True


class TestInfraFailuresDoNotCountAsAttempts:
    """A broken verifier environment must never look like a verdict.

    These cover the failure that quarantined scan:b9bcbfbe and scan:b26c21bf:
    Bandit and pytest were pointed at a directory with no checkout in it, and
    the resulting noise was scored as a verified rejection.
    """

    def _ctx(self):
        return VerifyContext(
            fp="abc123",
            rule_id="B608",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ def safe():\n+     return 42",
            repo_path=TEST_REPO_PATH,
        )

    def _run(
        self, monkeypatch, bandit_stdout, test_returncode, test_stderr="",
        run_tests_gate=True,
    ):
        # The checkout is exercised in TestPRCheckout; here we only care that
        # gates 3 and 4 classify their own failures correctly.
        #
        # The tests gate is off in the shipped config, so it is switched on
        # explicitly here - these cases exist to pin its behaviour.
        monkeypatch.setattr(config, "RUN_TESTS_GATE", run_tests_gate)
        monkeypatch.setattr(
            verifier, "_checkout_pr", lambda ctx, ev: (TEST_REPO_PATH, None)
        )
        monkeypatch.setattr(verifier, "_cleanup_checkout", lambda path: None)

        def fake_run(cmd, **kwargs):
            if "bandit" in cmd[0]:
                return subprocess.CompletedProcess(cmd, 0, bandit_stdout, "")
            return subprocess.CompletedProcess(cmd, test_returncode, "", test_stderr)

        monkeypatch.setattr(verifier.subprocess, "run", fake_run)
        return verify(self._ctx())

    # Bandit reporting a missing target, as it did in production.
    MISSING_TARGET = json.dumps({
        "errors": [{"filename": "./superset/", "reason": "No such file or directory"}],
        "metrics": {"_totals": {"loc": 0}},
        "results": [],
    })

    CLEAN_SCAN = json.dumps({
        "errors": [],
        "metrics": {"_totals": {"loc": 5000}},
        "results": [],
    })

    def test_bandit_scanning_nothing_is_infra_not_a_pass(self, monkeypatch):
        """An empty result set from an unreadable target must not clear the oracle."""
        verdict = self._run(monkeypatch, self.MISSING_TARGET, 0)

        assert verdict.passed is False
        assert verdict.gate == "infra"
        assert verdict.counts_as_attempt is False
        assert "oracle" not in verdict.evidence["gates_passed"]

    def test_bandit_zero_loc_without_errors_is_infra(self, monkeypatch):
        """Scanning zero lines proves nothing even if Bandit reports no errors."""
        empty = json.dumps({"errors": [], "metrics": {"_totals": {"loc": 0}}, "results": []})
        verdict = self._run(monkeypatch, empty, 0)

        assert verdict.gate == "infra"
        assert verdict.counts_as_attempt is False

    def test_missing_pytest_is_infra_not_a_test_failure(self, monkeypatch):
        """`python -m pytest` returns 1 when pytest is absent, not FileNotFoundError."""
        verdict = self._run(
            monkeypatch, self.CLEAN_SCAN, 1,
            test_stderr="/usr/local/bin/python: No module named pytest\n",
        )

        assert verdict.gate == "infra"
        assert verdict.counts_as_attempt is False
        assert "oracle" in verdict.evidence["gates_passed"]

    @pytest.mark.parametrize("code", [2, 3, 4, 5])
    def test_pytest_non_failure_exit_codes_are_infra(self, monkeypatch, code):
        """Interrupted, internal error, bad usage and no-tests-collected are all infra."""
        verdict = self._run(monkeypatch, self.CLEAN_SCAN, code)

        assert verdict.gate == "infra"
        assert verdict.counts_as_attempt is False

    def test_real_test_failure_still_counts_as_attempt(self, monkeypatch):
        """Exit 1 with pytest present is a genuine rejection."""
        verdict = self._run(monkeypatch, self.CLEAN_SCAN, 1, test_stderr="")

        assert verdict.passed is False
        assert verdict.gate == "tests"
        assert verdict.counts_as_attempt is True

    def test_untracked_rules_are_not_new_findings(self, monkeypatch):
        """Findings from rules outside RULE_ALLOWLIST were never in the ledger.

        The scanner only records allowlisted rules, so an unfiltered comparison
        makes every pre-existing B101/B110/B311 look like this PR introduced it.
        This rejected a correct fix to bigquery.py with 185 phantom findings.
        """
        noisy = json.dumps({
            "errors": [],
            "metrics": {"_totals": {"loc": 5000}},
            "results": [
                {"test_id": "B101", "filename": "superset/a.py", "code": "assert x"},
                {"test_id": "B110", "filename": "superset/b.py", "code": "pass"},
                {"test_id": "B311", "filename": "superset/c.py", "code": "random()"},
            ],
        })
        verdict = self._run(monkeypatch, noisy, 0)

        assert verdict.passed is True
        assert verdict.evidence["bandit_finding_count"] == 0
        assert verdict.evidence["bandit_total_findings_unfiltered"] == 3

    def test_new_finding_in_a_tracked_rule_still_rejects(self, monkeypatch):
        """Filtering must not blind the gate to genuine regressions."""
        regression = json.dumps({
            "errors": [],
            "metrics": {"_totals": {"loc": 5000}},
            "results": [
                {"test_id": "B101", "filename": "superset/a.py", "code": "assert x"},
                {"test_id": "B608", "filename": "superset/new.py", "code": "f'SELECT {x}'"},
            ],
        })
        verdict = self._run(monkeypatch, regression, 0)

        assert verdict.passed is False
        assert verdict.gate == "oracle"
        assert "new finding" in verdict.reason

    def test_clean_run_passes_all_gates(self, monkeypatch):
        """A readable scan with no findings and green tests passes."""
        verdict = self._run(monkeypatch, self.CLEAN_SCAN, 0)

        assert verdict.passed is True
        assert verdict.evidence["gates_passed"] == ["join", "policy", "oracle", "tests"]
        assert not verdict.evidence.get("gates_skipped")

    def test_skipped_tests_gate_is_never_reported_as_passed(self, monkeypatch):
        """With the tests gate off, a pass must not claim the tests ran.

        Anything downstream that reads gates_passed is deciding how much
        "verified" is worth, so the list has to stay literally true.
        """
        verdict = self._run(monkeypatch, self.CLEAN_SCAN, 0, run_tests_gate=False)

        assert verdict.passed is True
        assert verdict.evidence["gates_passed"] == ["join", "policy", "oracle"]
        assert "tests" not in verdict.evidence["gates_passed"]
        assert verdict.evidence["gates_skipped"] == ["tests"]
        # The human-readable reason must not claim the tests gate either.
        assert "oracle" in verdict.reason
        assert "tests" not in verdict.reason


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def pr_repos(tmp_path):
    """A master checkout plus a 'remote' carrying one PR under refs/pull/123/head.

    The remote is a local path, so these tests need no network.
    """
    source = tmp_path / "source"
    source.mkdir()
    _git(["init", "-q", "-b", "master"], source)
    _git(["config", "user.email", "t@example.com"], source)
    _git(["config", "user.name", "Test"], source)
    (source / "app.py").write_text("QUERY = 'SELECT 1'\n")
    _git(["add", "-A"], source)
    _git(["commit", "-qm", "master commit"], source)
    master_sha = _git(["rev-parse", "HEAD"], source).stdout.strip()

    # Build the PR commit on a branch, then publish it the way GitHub does.
    _git(["checkout", "-q", "-b", "pr-branch"], source)
    (source / "app.py").write_text("QUERY = 'SELECT 2'  # fixed\n")
    _git(["commit", "-qam", "pr commit"], source)
    pr_sha = _git(["rev-parse", "HEAD"], source).stdout.strip()
    _git(["checkout", "-q", "master"], source)
    _git(["branch", "-q", "-D", "pr-branch"], source)

    remote = tmp_path / "remote.git"
    _git(["clone", "-q", "--bare", str(source), str(remote)], tmp_path)
    _git(["update-ref", "refs/pull/123/head", pr_sha], remote)

    return {"source": source, "remote": remote, "pr_sha": pr_sha, "master_sha": master_sha}


class TestPRCheckout:
    """The gates must run against the PR's code, not the shared checkout's branch."""

    def _ctx(self, repo_path, pr_head_sha):
        return VerifyContext(
            fp="scan:abc123",
            rule_id="B608",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha=pr_head_sha,
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=scan:abc123 -->",
            changed_files=["superset/file.py"],
            diff="",
            repo_path=repo_path,
        )

    def test_checkout_materializes_pr_code_not_master(self, monkeypatch, pr_repos):
        """The working tree must contain the PR's version of the file."""
        monkeypatch.setattr(verifier, "_remote_url", lambda: str(pr_repos["remote"]))
        evidence = {}

        work, failure = verifier._checkout_pr(
            self._ctx(pr_repos["source"], pr_repos["pr_sha"]), evidence
        )

        assert failure is None
        assert (work / "app.py").read_text() == "QUERY = 'SELECT 2'  # fixed\n"
        assert evidence["checkout_sha"] == pr_repos["pr_sha"]
        verifier._cleanup_checkout(work)

    def test_source_checkout_is_left_untouched(self, monkeypatch, pr_repos):
        """Verification must not mutate the operator's working tree."""
        monkeypatch.setattr(verifier, "_remote_url", lambda: str(pr_repos["remote"]))
        source = pr_repos["source"]

        work, failure = verifier._checkout_pr(
            self._ctx(source, pr_repos["pr_sha"]), {}
        )
        assert failure is None
        verifier._cleanup_checkout(work)

        assert (source / "app.py").read_text() == "QUERY = 'SELECT 1'\n"
        assert _git(["rev-parse", "HEAD"], source).stdout.strip() == pr_repos["master_sha"]
        assert _git(["status", "--porcelain"], source).stdout == ""

    def test_cleanup_removes_the_scratch_tree(self, monkeypatch, pr_repos):
        monkeypatch.setattr(verifier, "_remote_url", lambda: str(pr_repos["remote"]))

        work, _ = verifier._checkout_pr(self._ctx(pr_repos["source"], pr_repos["pr_sha"]), {})
        scratch = work.parent
        verifier._cleanup_checkout(work)

        assert not scratch.exists()

    def test_moved_pr_is_infra_not_a_rejection(self, monkeypatch, pr_repos):
        """If the PR head moved, the diff we gated on is not the code we'd test."""
        monkeypatch.setattr(verifier, "_remote_url", lambda: str(pr_repos["remote"]))

        work, failure = verifier._checkout_pr(
            self._ctx(pr_repos["source"], pr_repos["master_sha"]), {}
        )

        assert work is None
        assert failure.gate == "infra"
        assert failure.counts_as_attempt is False
        assert "moved during verification" in failure.reason

    def test_missing_head_sha_is_infra(self, pr_repos):
        work, failure = verifier._checkout_pr(self._ctx(pr_repos["source"], ""), {})

        assert work is None
        assert failure.gate == "infra"
        assert failure.counts_as_attempt is False

    def test_unfetchable_pr_is_infra(self, monkeypatch, pr_repos, tmp_path):
        """A remote that cannot serve the ref must not look like a failed fix."""
        monkeypatch.setattr(verifier, "_remote_url", lambda: str(tmp_path / "nope.git"))

        work, failure = verifier._checkout_pr(
            self._ctx(pr_repos["source"], pr_repos["pr_sha"]), {}
        )

        assert work is None
        assert failure.gate == "infra"
        assert failure.counts_as_attempt is False

    def test_token_is_redacted_from_failure_reasons(self, monkeypatch, pr_repos, tmp_path):
        """Checkout errors quote git stderr, which can carry the remote URL."""
        monkeypatch.setattr(verifier.config, "GITHUB_TOKEN", "ghp_supersecrettoken")
        monkeypatch.setattr(
            verifier, "_remote_url", lambda: str(tmp_path / "ghp_supersecrettoken.git")
        )

        _, failure = verifier._checkout_pr(
            self._ctx(pr_repos["source"], pr_repos["pr_sha"]), {}
        )

        assert "ghp_supersecrettoken" not in failure.reason
        assert "***" in failure.reason


class TestEvidenceCollection:
    """Tests for evidence collection in verdicts."""
    
    def test_evidence_contains_changed_files(self):
        """Evidence should contain the changed files list."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py", "tests/test_file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ new line",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        assert "changed_files" in verdict.evidence
        assert verdict.evidence["changed_files"] == ["superset/file.py", "tests/test_file.py"]
    
    def test_evidence_contains_diff_line_counts(self):
        """Evidence should contain diff line counts for gate 2."""
        ctx = VerifyContext(
            fp="abc123",
            rule_id="B101",
            issue_number=42,
            branch="cognition-project/abc123",
            pr_number=123,
            pr_head_sha="def456",
            pr_body="Fixes #42\n\n<!-- cognition-project:fp=abc123 -->",
            changed_files=["superset/file.py"],
            diff="diff --git a/superset/file.py b/superset/file.py\n+ line1\n+ line2\n- old_line",
            repo_path=TEST_REPO_PATH,
        )
        
        verdict = verify(ctx)
        
        # If gate 2 was reached, evidence should contain line counts
        if verdict.gate not in ["join"]:
            assert "diff_added_lines" in verdict.evidence
            assert "diff_removed_lines" in verdict.evidence
            assert "diff_total_loc" in verdict.evidence


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
