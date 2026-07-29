"""Scanner module for cognition-project.

This module runs Bandit scans, fingerprints findings, and syncs them to the ledger.
"""

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from cognition.core import config, db
from cognition.api import github


@dataclass
class Finding:
    """A Bandit finding."""
    test_id: str
    file_path: str
    line_number: int
    code: str
    issue_text: str
    severity: str


def fingerprint(rule_id: str, path: str, code: str) -> str:
    """Generate a fingerprint for a finding.
    
    Line numbers are deliberately excluded so that an unrelated edit fifty lines above 
    does not mint a new "problem."
    
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

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    log = logging.getLogger(__name__)

    cfg = config.cfg
    repo_path = cfg.SUPERSET_PATH  # or wherever your fork is cloned

    log.info("Starting scan of %s", repo_path)
    findings = scan(repo_path)
    log.info("Found %d findings after allowlist filter", len(findings))

    for f in findings:
        fp = fingerprint(f.test_id, f.file_path, f.code)
        log.info("  %s  %s  %s:%d", fp, f.test_id, f.file_path, f.line_number)

    sync_findings(findings)
    log.info("Sync complete")