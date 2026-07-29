"""Verification module for cognition-project.

This module answers one question: did the change the agent claims it made
actually do what it says? It operates with no network and no API keys,
making it testable against hand-written diffs.
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from scanner import fingerprint


@dataclass
class VerifyContext:
    """Context for verification."""
    fp: str
    rule_id: str
    issue_number: int
    branch: str
    pr_number: int
    pr_head_sha: str
    pr_body: str
    changed_files: List[str]
    diff: str
    repo_path: Path
    known_fingerprints: List[str] = field(default_factory=list)


@dataclass
class Verdict:
    """Result of verification."""
    passed: bool
    gate: Optional[str]  # Which gate failed, or None if passed
    reason: str  # Human-readable explanation
    evidence: Dict[str, Any] = field(default_factory=dict)
    counts_as_attempt: bool = True  # Whether this counts against attempt cap


def verify(ctx: VerifyContext) -> Verdict:
    """Run all verification gates and return a verdict.
    
    Gates run cheapest first and stop at the first failure.
    Never spends a test run on a diff that already violates policy.
    
    Args:
        ctx: Verification context.
        
    Returns:
        Verdict indicating whether the change is verified.
    """
    evidence: Dict[str, Any] = {
        "gates_passed": [],
        "changed_files": ctx.changed_files,
    }
    
    # Gate 1: Join
    result = _gate_join(ctx, evidence)
    if not result.passed:
        return result
    
    # Gate 2: Policy
    result = _gate_policy(ctx, evidence)
    if not result.passed:
        return result
    
    # Gate 3: Oracle
    result = _gate_oracle(ctx, evidence)
    if not result.passed:
        return result
    
    # Gate 4: Tests
    result = _gate_tests(ctx, evidence)
    if not result.passed:
        return result
    
    # All gates passed
    evidence["gates_passed"] = ["join", "policy", "oracle", "tests"]
    return Verdict(
        passed=True,
        gate=None,
        reason="All verification gates passed.",
        evidence=evidence,
        counts_as_attempt=True,
    )


def _gate_join(ctx: VerifyContext, evidence: Dict[str, Any]) -> Verdict:
    """Gate 1: Validate PR body and branch name.
    
    The PR body must contain:
    - Fixes #<issue_number>
    - Footer marker <!-- cognition-project:fp=<fp> -->
    
    The branch name must equal cognition-project/<fp>.
    
    If any fail: the PR is not provably the artifact for this task.
    """
    expected_branch = f"cognition-project/{ctx.fp}"
    expected_fixes = f"Fixes #{ctx.issue_number}"
    expected_footer = f"<!-- cognition-project:fp={ctx.fp} -->"
    
    if expected_fixes not in ctx.pr_body:
        return Verdict(
            passed=False,
            gate="join",
            reason=f"PR body must contain '{expected_fixes}'",
            evidence=evidence,
            counts_as_attempt=True,
        )
    
    if expected_footer not in ctx.pr_body:
        return Verdict(
            passed=False,
            gate="join",
            reason=f"PR body must contain footer '{expected_footer}'",
            evidence=evidence,
            counts_as_attempt=True,
        )
    
    if ctx.branch != expected_branch:
        return Verdict(
            passed=False,
            gate="join",
            reason=f"Branch name must be '{expected_branch}'",
            evidence=evidence,
            counts_as_attempt=True,
        )
    
    evidence["gates_passed"].append("join")
    return Verdict(
        passed=True,
        gate=None,
        reason="",
        evidence=evidence,
        counts_as_attempt=True,
    )


def _gate_policy(ctx: VerifyContext, evidence: Dict[str, Any]) -> Verdict:
    """Gate 2: Validate diff content and file list.
    
    Rules:
    - Every changed file starts with one of cfg.PATH_ALLOWLIST
    - Total added + removed lines ≤ cfg.MAX_DIFF_LOC
    - No added line contains # nosec, # noqa, or # type: ignore
    - No changed file is a dependency file
    
    Removed lines don't count for suppression checks.
    """
    # Check for dependency files first (more specific check)
    dependency_files = [
        "requirements.txt",
        "requirements.in",
        "requirements.pip",
        "setup.py",
        "setup.cfg",
        "pyproject.toml",
        "poetry.lock",
        "package.json",
        "package-lock.json",
    ]
    
    for file_path in ctx.changed_files:
        file_name = Path(file_path).name
        if file_name in dependency_files:
            return Verdict(
                passed=False,
                gate="policy",
                reason=f"Cannot modify dependency file '{file_path}'",
                evidence=evidence,
                counts_as_attempt=True,
            )
    
    # Check file paths
    for file_path in ctx.changed_files:
        if not any(file_path.startswith(prefix) for prefix in config.PATH_ALLOWLIST):
            return Verdict(
                passed=False,
                gate="policy",
                reason=f"File '{file_path}' is not in allowed paths",
                evidence=evidence,
                counts_as_attempt=True,
            )
    
    # Count lines and check for suppressions
    added_lines = 0
    removed_lines = 0
    suppression_patterns = ["# nosec", "# noqa", "# type: ignore"]
    
    for line in ctx.diff.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added_lines += 1
            # Check for suppressions in added lines
            for pattern in suppression_patterns:
                if pattern in line:
                    return Verdict(
                        passed=False,
                        gate="policy",
                        reason="You added a suppression comment. That is not a fix. Fix the underlying finding.",
                        evidence=evidence,
                        counts_as_attempt=True,
                    )
        elif line.startswith("-") and not line.startswith("---"):
            removed_lines += 1
    
    total_loc = added_lines + removed_lines
    evidence["diff_added_lines"] = added_lines
    evidence["diff_removed_lines"] = removed_lines
    evidence["diff_total_loc"] = total_loc
    
    if total_loc > config.MAX_DIFF_LOC:
        return Verdict(
            passed=False,
            gate="policy",
            reason=f"Diff too large: {total_loc} lines (max {config.MAX_DIFF_LOC})",
            evidence=evidence,
            counts_as_attempt=True,
        )
    
    evidence["gates_passed"].append("policy")
    return Verdict(
        passed=True,
        gate=None,
        reason="",
        evidence=evidence,
        counts_as_attempt=True,
    )


def _gate_oracle(ctx: VerifyContext, evidence: Dict[str, Any]) -> Verdict:
    """Gate 3: Run Bandit and verify fingerprints.
    
    Requires that:
    - The finding's fingerprint is absent
    - No new fingerprints appeared
    
    Uses the identical pinned command the scanner uses.
    """
    evidence["bandit_command"] = config.BANDIT_CMD
    
    # Check if repo path exists (for testing purposes)
    if not ctx.repo_path.exists():
        return Verdict(
            passed=False,
            gate="infra",
            reason=f"Repository path does not exist: {ctx.repo_path}",
            evidence=evidence,
            counts_as_attempt=False,  # Infra failure, not a verified rejection
        )
    
    try:
        result = subprocess.run(
            config.BANDIT_CMD.split(),
            cwd=ctx.repo_path,
            capture_output=True,
            text=True,
            timeout=config.BANDIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return Verdict(
            passed=False,
            gate="infra",
            reason="Bandit execution timed out",
            evidence=evidence,
            counts_as_attempt=False,  # Infra failure, not a verified rejection
        )
    except FileNotFoundError:
        return Verdict(
            passed=False,
            gate="infra",
            reason="Bandit executable not found",
            evidence=evidence,
            counts_as_attempt=False,  # Infra failure, not a verified rejection
        )
    
    evidence["bandit_exit_code"] = result.returncode
    evidence["bandit_stdout"] = result.stdout
    evidence["bandit_stderr"] = result.stderr
    
    # Parse Bandit output
    try:
        bandit_output = json.loads(result.stdout)
    except json.JSONDecodeError:
        return Verdict(
            passed=False,
            gate="oracle",
            reason="Failed to parse Bandit output",
            evidence=evidence,
            counts_as_attempt=True,
        )
    
    # Extract fingerprints
    findings = bandit_output.get("results", [])
    current_fingerprints = {
        fingerprint(finding.get("test_id"), finding.get("file_path"), finding.get("code"))
        for finding in findings
    }
    
    evidence["bandit_fingerprints_after"] = list(current_fingerprints)
    evidence["bandit_finding_count"] = len(findings)

    # The fingerprint this task was opened for must be gone.
    if ctx.fp in current_fingerprints:
        return Verdict(
            passed=False,
            gate="oracle",
            reason=f"Fingerprint {ctx.fp} is still present after the fix. The finding was not resolved.",
            evidence=evidence,
            counts_as_attempt=True,
        )

    # Any fingerprint that isn't already known to the ledger is a new finding
    # introduced by this change.
    baseline = set(ctx.known_fingerprints) | {ctx.fp}
    new_fingerprints = current_fingerprints - baseline
    if new_fingerprints:
        evidence["bandit_new_fingerprints"] = sorted(new_fingerprints)
        return Verdict(
            passed=False,
            gate="oracle",
            reason=f"Fix introduced {len(new_fingerprints)} new finding(s): {', '.join(sorted(new_fingerprints))}",
            evidence=evidence,
            counts_as_attempt=True,
        )

    evidence["gates_passed"].append("oracle")
    return Verdict(
        passed=True,
        gate=None,
        reason="",
        evidence=evidence,
        counts_as_attempt=True,
    )


def _gate_tests(ctx: VerifyContext, evidence: Dict[str, Any]) -> Verdict:
    """Gate 4: Run the mapped test subset for the touched files.
    
    Uses the mapping in config.TEST_MAPPING, defaulting to tests/unit_tests.
    Green (exit code 0) is required.
    """
    # Determine which test subset to run
    test_path = config.TEST_MAPPING.get("default", "tests/unit_tests")
    
    # Build test command
    test_cmd = ["python", "-m", "pytest", test_path, "-v"]
    
    evidence["test_command"] = " ".join(test_cmd)
    
    # Check if repo path exists (for testing purposes)
    if not ctx.repo_path.exists():
        return Verdict(
            passed=False,
            gate="infra",
            reason=f"Repository path does not exist: {ctx.repo_path}",
            evidence=evidence,
            counts_as_attempt=False,  # Infra failure, not a verified rejection
        )
    
    try:
        result = subprocess.run(
            test_cmd,
            cwd=ctx.repo_path,
            capture_output=True,
            text=True,
            timeout=config.TEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return Verdict(
            passed=False,
            gate="infra",
            reason="Test execution timed out",
            evidence=evidence,
            counts_as_attempt=False,  # Infra failure, not a verified rejection
        )
    except FileNotFoundError:
        return Verdict(
            passed=False,
            gate="infra",
            reason="Test executable not found",
            evidence=evidence,
            counts_as_attempt=False,  # Infra failure, not a verified rejection
        )
    
    evidence["test_exit_code"] = result.returncode
    evidence["test_stdout"] = result.stdout
    evidence["test_stderr"] = result.stderr
    
    if result.returncode != 0:
        return Verdict(
            passed=False,
            gate="tests",
            reason="Tests failed",
            evidence=evidence,
            counts_as_attempt=True,
        )
    
    evidence["gates_passed"].append("tests")
    return Verdict(
        passed=True,
        gate=None,
        reason="",
        evidence=evidence,
        counts_as_attempt=True,
    )
