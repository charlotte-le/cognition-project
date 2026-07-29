"""Verification module for cognition-project.

This module answers one question: did the change the agent claims it made
actually do what it says? It operates with no network and no API keys,
making it testable against hand-written diffs.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from cognition.core import config
from cognition.verification import scanner


# pytest exit codes. Only TESTS_FAILED is a statement about the change under
# review; the rest mean the run never produced a usable answer.
PYTEST_OK = 0
PYTEST_TESTS_FAILED = 1
PYTEST_INFRA_CODES = {
    2: "test run was interrupted",
    3: "internal error in pytest",
    4: "pytest usage error (test path likely missing)",
    5: "no tests were collected",
}


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
    
    # Gates 3 and 4 must run against the code the PR proposes, not against
    # whatever branch the shared checkout happens to be sitting on.
    scratch, checkout_failure = _checkout_pr(ctx, evidence)
    if checkout_failure is not None:
        return checkout_failure

    try:
        pr_ctx = replace(ctx, repo_path=scratch)

        # Gate 3: Oracle
        result = _gate_oracle(pr_ctx, evidence)
        if not result.passed:
            return result

        # Gate 4: Tests, when the environment can actually run them.
        if config.RUN_TESTS_GATE:
            result = _gate_tests(pr_ctx, evidence)
            if not result.passed:
                return result
        else:
            evidence["gates_skipped"] = ["tests"]
            evidence["tests_skipped_reason"] = (
                "RUN_TESTS_GATE is off: this verifier does not carry the target "
                "repo's dependencies, so it cannot run its test suite."
            )
    finally:
        _cleanup_checkout(scratch)

    # gates_passed is whatever the gates themselves appended. Never assert that
    # a gate ran when it did not - a caller reading this list is deciding how
    # much the word "verified" is worth.
    return Verdict(
        passed=True,
        gate=None,
        reason=f"Verification gates passed: {', '.join(evidence['gates_passed'])}.",
        evidence=evidence,
        counts_as_attempt=True,
    )


def _redact(text: str) -> str:
    """Strip the GitHub token out of anything headed for logs or evidence."""
    if config.GITHUB_TOKEN:
        return text.replace(config.GITHUB_TOKEN, "***")
    return text


def _remote_url() -> str:
    """Clone URL for the repo the PR lives on.

    Deliberately not the local checkout's origin: the target repo is a fork,
    and refs/pull/N/head means a different PR on every other remote.
    """
    if config.GITHUB_TOKEN:
        return f"https://x-access-token:{config.GITHUB_TOKEN}@github.com/{config.GITHUB_REPO}.git"
    return f"https://github.com/{config.GITHUB_REPO}.git"


def _git(args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=config.CHECKOUT_TIMEOUT,
    )


def _checkout_failure(reason: str, evidence: Dict[str, Any], scratch: Optional[Path]) -> Verdict:
    """Every checkout problem is infra: it says nothing about the agent's work."""
    _cleanup_checkout(scratch)
    return Verdict(
        passed=False,
        gate="infra",
        reason=_redact(reason),
        evidence=evidence,
        counts_as_attempt=False,
    )


def _checkout_pr(
    ctx: VerifyContext, evidence: Dict[str, Any]
) -> tuple[Optional[Path], Optional[Verdict]]:
    """Materialize the PR's code in a throwaway working tree.

    Fetches only the PR's head ref, shallow, into a fresh repository. The
    operator's checkout is never read or written, so concurrent verifications
    cannot collide and nothing local can contaminate the result: what gets
    tested is exactly what the remote says the PR is.

    Returns (path, None) on success, or (None, verdict) on failure.
    """
    if not ctx.pr_head_sha:
        return None, _checkout_failure(
            f"PR #{ctx.pr_number} has no head SHA; cannot pin the code under review",
            evidence,
            None,
        )

    scratch = Path(tempfile.mkdtemp(prefix=f"verify-{ctx.fp.replace(':', '-')}-"))
    work = scratch / "repo"
    work.mkdir()

    result = _git(["init", "-q"], cwd=work)
    if result.returncode != 0:
        return None, _checkout_failure(
            f"Could not initialize scratch repo: {result.stderr.strip()}", evidence, scratch
        )

    # refs/pull/N/head is stable even if the branch is later deleted.
    result = _git(["fetch", "--depth=1", _remote_url(), f"refs/pull/{ctx.pr_number}/head"], cwd=work)
    if result.returncode != 0:
        return None, _checkout_failure(
            f"Could not fetch PR #{ctx.pr_number} from {config.GITHUB_REPO}: "
            f"{result.stderr.strip()}",
            evidence,
            scratch,
        )

    fetched = _git(["rev-parse", "FETCH_HEAD"], cwd=work).stdout.strip()

    # The reconciler recorded a SHA when it read the PR. If what we fetched is
    # a different commit, the PR moved underneath us and the diff that cleared
    # gates 1 and 2 is not the code we are about to test.
    if fetched != ctx.pr_head_sha:
        return None, _checkout_failure(
            f"PR #{ctx.pr_number} moved during verification: expected "
            f"{ctx.pr_head_sha}, fetched {fetched}",
            evidence,
            scratch,
        )

    result = _git(["checkout", "--detach", fetched], cwd=work)
    if result.returncode != 0:
        return None, _checkout_failure(
            f"Could not check out {fetched}: {result.stderr.strip()}", evidence, scratch
        )

    evidence["checkout_sha"] = fetched
    evidence["checkout_source"] = f"{config.GITHUB_REPO}#{ctx.pr_number}"
    return work, None


def _cleanup_checkout(path: Optional[Path]) -> None:
    """Remove the scratch tree. Never fatal - a leaked temp dir is not a verdict."""
    if path is None:
        return
    # _checkout_pr works in <scratch>/repo; remove the whole scratch dir.
    target = path.parent if path.name == "repo" else path
    shutil.rmtree(target, ignore_errors=True)


def _gate_join(ctx: VerifyContext, evidence: Dict[str, Any]) -> Verdict:
    """Gate 1: Validate PR body and branch name.
    
    The PR body must contain:
    - Fixes #<issue_number>
    - Footer marker <!-- cognition-project:fp=<fp> -->
    
    The branch name must equal cognition-project/<fp>.
    
    If any fail: the PR is not provably the artifact for this task.
    """
    expected_branch = scanner.branch_name(ctx.fp)
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

    # A scan that scanned nothing proves nothing. --exit-zero makes Bandit
    # return 0 even when the target path is missing, and the empty result set
    # that comes back would otherwise clear every gate below it.
    scan_errors = bandit_output.get("errors") or []
    scanned_loc = bandit_output.get("metrics", {}).get("_totals", {}).get("loc", 0)
    evidence["bandit_scan_errors"] = scan_errors
    evidence["bandit_scanned_loc"] = scanned_loc

    if scan_errors or not scanned_loc:
        detail = scan_errors[0].get("reason", "unknown") if scan_errors else "no source read"
        return Verdict(
            passed=False,
            gate="infra",
            reason=(
                f"Bandit scanned {scanned_loc} lines from {ctx.repo_path} "
                f"({detail}). The scan target is unreadable, so the absence of "
                f"fingerprint {ctx.fp} proves nothing."
            ),
            evidence=evidence,
            counts_as_attempt=False,  # Infra failure, not a verified rejection
        )

    # Extract fingerprints. This must mirror scanner.scan() exactly: same rule
    # filter, same JSON keys. The ledger only ever contains allowlisted rules,
    # so comparing an unfiltered scan against it makes every finding from every
    # untracked rule look like something this PR introduced.
    findings = [
        f for f in bandit_output.get("results", [])
        if f.get("test_id") in config.RULE_ALLOWLIST
    ]
    current_fingerprints = {
        scanner.fingerprint(
            finding.get("test_id", ""),
            finding.get("filename", ""),
            finding.get("code", ""),
        )
        for finding in findings
    }

    evidence["bandit_fingerprints_after"] = list(current_fingerprints)
    evidence["bandit_finding_count"] = len(findings)
    evidence["bandit_total_findings_unfiltered"] = len(bandit_output.get("results", []))

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
    # Determine which test subset to run - check rule-specific mapping first
    test_path = config.TEST_MAPPING.get(ctx.rule_id, config.TEST_MAPPING.get("default", "tests/unit_tests"))
    
    # Build test command. sys.executable rather than "python", which does not
    # exist on hosts where only python3 is on PATH.
    test_cmd = [sys.executable, "-m", "pytest", test_path, "-v"]
    
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
    
    if result.returncode != PYTEST_OK:
        # `python -m pytest` always finds `python`, so a missing pytest comes
        # back as exit 1 rather than the FileNotFoundError caught above.
        if "No module named pytest" in result.stderr:
            return Verdict(
                passed=False,
                gate="infra",
                reason=f"pytest is not installed in the verifier environment ({ctx.repo_path})",
                evidence=evidence,
                counts_as_attempt=False,  # Infra failure, not a verified rejection
            )

        if result.returncode in PYTEST_INFRA_CODES:
            return Verdict(
                passed=False,
                gate="infra",
                reason=(
                    f"Test run did not complete: "
                    f"{PYTEST_INFRA_CODES[result.returncode]} "
                    f"(pytest exit {result.returncode}, path '{test_path}')"
                ),
                evidence=evidence,
                counts_as_attempt=False,  # Infra failure, not a verified rejection
            )

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
