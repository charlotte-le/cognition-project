"""Tests for verifier.py using fixture diffs.

These tests run offline with hand-crafted diffs to verify all gates work correctly.
"""

import pytest
from pathlib import Path
from verifier import VerifyContext, verify

# Use current directory for tests that need a valid path
TEST_REPO_PATH = Path("/Users/charlottele/Desktop/cognition-project")


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
