"""Scanner module for cognition-project.

This module runs Bandit scans, fingerprints findings, and syncs them to the ledger.
"""

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from cognition.core import config, db
from cognition.api import github

logger = logging.getLogger(__name__)


# Liveness for the scheduled scan, mirroring the reconciler's tick globals.
# Without these the status page can say the reconciler is alive while the trigger
# that feeds it has been dead for hours.
_last_scan_time: Optional[datetime] = None
_last_scan_findings: Optional[int] = None
_last_scan_error: Optional[str] = None


def get_last_scan_time() -> Optional[datetime]:
    """When the last scan completed, or None if none has run this process."""
    return _last_scan_time


def get_last_scan_findings() -> Optional[int]:
    """How many allowlisted findings the last scan returned."""
    return _last_scan_findings


def get_last_scan_error() -> Optional[str]:
    """Why the last scan failed, or None if it succeeded."""
    return _last_scan_error


@dataclass
class Finding:
    """A Bandit finding."""
    test_id: str
    file_path: str
    line_number: int
    code: str
    issue_text: str
    severity: str


# Bandit renders a finding's `code` as one "<line_no> <source line>" per line of
# the finding's range. Those numbers are part of the string, so anything that
# shifts the finding down the file changes the text without changing the code.
_BANDIT_LINE_PREFIX = re.compile(r"^\s*\d+\s")


def normalize_code(code: str) -> str:
    """Strip Bandit's line-number prefixes from a finding's code snippet.

    This is what makes a finding's identity survive an edit above it. Hashing
    Bandit's `code` verbatim ties the identity to the finding's position in the
    file, so a fix that adds a line renames every finding below it and an
    upstream merge renames findings nobody touched.

    Args:
        code: The `code` field from Bandit's JSON output.

    Returns:
        The same snippet with the line numbers removed.
    """
    return "\n".join(
        _BANDIT_LINE_PREFIX.sub("", line) for line in code.splitlines()
    ).strip()


def finding_key(rule_id: str, path: str, code: str) -> str:
    """Position-independent identity for a finding.

    Two findings share a key when they are the same rule firing on the same
    source text in the same file, regardless of what line it currently sits on.
    This is the only sound basis for asking "is this finding new?" - see
    verifier._gate_oracle.

    Distinct from fingerprint(): fingerprints are the ledger's durable primary
    key and are already embedded in filed GitHub issues and branch names, so
    they cannot be redefined without migrating those. Keys are computed fresh on
    every comparison and are never persisted.
    """
    return f"{rule_id}|{path.replace(chr(92), '/')}|{normalize_code(code)}"


def fingerprint(rule_id: str, path: str, code: str) -> str:
    """Generate a fingerprint for a finding.

    Note: `code` carries Bandit's line-number prefixes, so this value moves when
    the finding moves. That is tolerable for a ledger primary key, which only has
    to be stable for as long as the row is open, but it is not a safe basis for
    comparing two scans - use finding_key() for that.

    Args:
        rule_id: The Bandit test ID (e.g., "B608").
        path: The normalized file path.
        code: The code snippet that triggered the finding.
        
    Returns:
        A fingerprint string in the format "scan:" + 8-character hex digest.
    """
    # Normalize the path to ensure consistency
    normalized_path = path.replace("\\", "/")
    
    # Create the fingerprint string
    fingerprint_str = f"{rule_id}|{normalized_path}|{code.strip()}"
    
    # Generate SHA256 hash and take first 8 characters
    hash_digest = hashlib.sha256(fingerprint_str.encode()).hexdigest()[:8]
    
    return f"scan:{hash_digest}"


def branch_name(fp: str) -> str:
    """Build the git branch name for a task's fingerprint.

    Git ref names cannot contain ':', so the fingerprint's colon is replaced
    with a hyphen. This must be the single source of truth for that mapping —
    prompts.py, reconciler.py, and verifier.py all need the identical result.
    """
    return f"cognition-project/{fp.replace(':', '-')}"


def scan(repo_path: str) -> List[Finding]:
    """Run Bandit scan and return filtered findings.
    
    Runs the pinned Bandit command from config, parses the JSON output,
    and keeps only findings whose test_id is in config.RULE_ALLOWLIST.
    
    Args:
        repo_path: Path to the repository to scan.
        
    Returns:
        List of Finding objects.
    """
    try:
        result = subprocess.run(
            config.BANDIT_CMD.split(),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=config.BANDIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return []
    except FileNotFoundError:
        return []
    
    # Parse Bandit output
    try:
        bandit_output = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    
    # Filter findings by RULE_ALLOWLIST
    findings = []
    for result_item in bandit_output.get("results", []):
        test_id = result_item.get("test_id")
        if test_id not in config.RULE_ALLOWLIST:
            continue
        
        finding = Finding(
            test_id=test_id,
            # Bandit's JSON key is "filename"; there is no "file_path".
            file_path=result_item.get("filename", ""),
            line_number=result_item.get("line_number", 0),
            code=result_item.get("code", ""),
            issue_text=result_item.get("issue_text", ""),
            severity=result_item.get("issue_severity", ""),
        )
        findings.append(finding)
    
    return findings


def sync_findings(findings: List[Finding]) -> None:
    """Sync findings to the ledger and create GitHub issues.
    
    For each finding:
    - Upsert a task in the database using the fingerprint
    - If and only if a row was newly inserted, create a GitHub issue and store its number
    
    The issue body must contain:
    - Rule ID
    - File path
    - Code snippet
    - Why it matters
    - Hidden marker <!-- cognition-project:fp=<fp> -->
    
    Label it cognition-project:auto.
    
    Args:
        findings: List of Finding objects to sync.
    """
    github_client = github.get_client()
    
    for finding in findings:
        # Generate fingerprint
        fp = fingerprint(finding.test_id, finding.file_path, finding.code)
        
        # Build payload
        payload = {
            "test_id": finding.test_id,
            "file_path": finding.file_path,
            "line_number": finding.line_number,
            "code": finding.code,
            "issue_text": finding.issue_text,
            "severity": finding.severity,
        }
        
        # Upsert task - returns True only if a row was inserted
        is_new = db.upsert_task(fp, finding.test_id, payload)
        
        if is_new:
            # Create GitHub issue
            title = f"[{finding.test_id}] {finding.file_path}:{finding.line_number}"
            
            body = f"""## Finding: {finding.test_id}

**File:** {finding.file_path}:{finding.line_number}
**Severity:** {finding.severity}

### Code
```python
{finding.code}
```

### Why it matters
{finding.issue_text}

<!-- cognition-project:fp={fp} -->
"""
            
            issue_number = github_client.create_issue(
                title=title,
                body=body,
                labels=["cognition-project:auto"]
            )
            
            # Update the task with the issue number
            db.transition(fp, db.State.PENDING, db.State.PENDING, issue_number=issue_number)


def run_scan(repo_path: Optional[Path] = None) -> int:
    """Scan the target repo and sync what it finds, recording liveness.

    This is the single entry point behind every scan trigger - the schedule, the
    Scan Now button, and the CLI - so all three record the same liveness and
    cannot drift apart.

    Blocking: it shells out to Bandit and talks to the GitHub API. Async callers
    must run it in a thread so the reconciler and the web server keep serving.

    Args:
        repo_path: Repo to scan. Defaults to config.TARGET_REPO_PATH.

    Returns:
        The number of allowlisted findings the scan returned.
    """
    global _last_scan_time, _last_scan_findings, _last_scan_error

    target = repo_path if repo_path is not None else config.TARGET_REPO_PATH

    try:
        findings = scan(target)
        sync_findings(findings)
    except Exception as e:
        # Recorded rather than swallowed: a scan that cannot run means the
        # pipeline is starving, and that has to be visible on the status page.
        _last_scan_time = datetime.utcnow()
        _last_scan_error = str(e)
        raise

    _last_scan_time = datetime.utcnow()
    _last_scan_findings = len(findings)
    _last_scan_error = None
    return len(findings)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")

    logger.info("Starting scan of %s", config.TARGET_REPO_PATH)
    count = run_scan()
    logger.info("Sync complete: %d findings after allowlist filter", count)