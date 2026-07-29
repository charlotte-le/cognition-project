"""Reconciler module for cognition-project.

This module implements the main orchestration loop that:
- Runs every TICK_SECONDS
- Reacts to state, not to events
- Takes at most one transition per task per tick
- Survives crashes (nothing important lives in memory)
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from cognition.core import config, db, prompts
from cognition.api import devin, github
from cognition.verification import scanner, verifier

logger = logging.getLogger(__name__)


# Global HALT latch
_halt_latched = False

# Global liveness tracking
_last_tick_time = None
_last_tick_exception = None
_orphaned_session_count = 0


def get_last_tick_time():
    """Get the last tick time for liveness monitoring."""
    return _last_tick_time


def get_last_tick_exception():
    """Get the last tick exception for liveness monitoring."""
    return _last_tick_exception


def get_orphaned_session_count():
    """Get the count of orphaned sessions for anomaly monitoring."""
    return _orphaned_session_count


# What a verification verdict means for the task's state.
OUTCOME_READY = "ready"
OUTCOME_RETRY = "retry"
OUTCOME_QUARANTINE = "quarantine"
OUTCOME_WAIT = "wait"


def _skipped_gates_note(verdict: verifier.Verdict) -> str:
    """Spell out any gate that did not run, so 'verified' is never read too broadly."""
    skipped = verdict.evidence.get("gates_skipped") or []
    if not skipped:
        return ""

    reason = verdict.evidence.get("tests_skipped_reason", "")
    return f"\n**Gates skipped:** {', '.join(skipped)}{f' — {reason}' if reason else ''}\n"


def decide_outcome(verdict: verifier.Verdict, attempt_count: int) -> str:
    """Map a verdict onto the transition it should cause.

    Split out from run_verification so this policy is testable without a GitHub
    client, a Devin client, or a database - the routing here is what decides
    whether real work gets thrown away.

    An infra verdict (counts_as_attempt False) is a statement about the
    verifier's own environment, not about the agent's work. It must neither burn
    an attempt nor quarantine: the task waits in VERIFYING and the gates re-run
    next tick, once the environment is fixed.
    """
    if verdict.passed:
        return OUTCOME_READY

    if not verdict.counts_as_attempt:
        return OUTCOME_WAIT

    if attempt_count < config.MAX_ATTEMPTS:
        return OUTCOME_RETRY

    return OUTCOME_QUARANTINE


def can_admit(task: Dict[str, Any]) -> Tuple[bool, str]:
    """Check if a task can be admitted for processing.
    
    Admission policy:
    - The class is in RULE_ALLOWLIST
    - db.count_active() < cfg.MAX_CONCURRENT
    - db.acus_today() < cfg.DAILY_ACU_CEILING
    - No HALT is latched
    
    Returns:
        Tuple of (admitted: bool, reason: str)
    """
    # Check HALT latch
    if _halt_latched:
        return False, "HALT latched - no new sessions allowed"
    
    # Check class allowlist
    cls = task.get("class", "")
    if cls not in config.RULE_ALLOWLIST:
        return False, f"Class {cls} not in allowlist"
    
    # Check concurrency
    active_count = db.count_active()
    if active_count >= config.MAX_CONCURRENT:
        return False, f"Concurrency limit reached ({active_count}/{config.MAX_CONCURRENT})"
    
    # Check daily ACU ceiling
    acus_today = db.acus_today()
    if acus_today >= config.DAILY_ACU_CEILING:
        return False, f"Daily ACU ceiling reached ({acus_today}/{config.DAILY_ACU_CEILING})"
    
    return True, "Admitted"


def harvest_pr(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Harvest PR information from a session.
    
    Extracts both structured_output (what the agent says happened) and 
    pull_requests[] (what the API observed).
    
    Args:
        session: Session dictionary from Devin API.
        
    Returns:
        Dictionary with structured_output and pull_requests, or None if no PR.
    """
    pull_requests = session.get("pull_requests", [])
    if not pull_requests:
        return None
    
    # Get the first PR (should only be one)
    pr = pull_requests[0]
    
    return {
        "structured_output": session.get("structured_output"),
        "pr_state": pr.get("pr_state"),
        "pr_url": pr.get("pr_url"),
    }


def move_to_verifying(fp: str, task: Dict[str, Any], session: Dict[str, Any], from_state: str) -> None:
    """Harvest a session's PR and transition the task to VERIFYING.

    A PR appearing on the session is treated as a stronger signal than the vendor's
    own status label — the human may merge/act on the PR directly on GitHub without
    ever resuming the session, so this is called from BLOCKED as well as RUNNING.

    Args:
        fp: Task fingerprint.
        task: Task dictionary from the database.
        session: Session dictionary from Devin API.
        from_state: The state the task is transitioning from.
    """
    pr_info = harvest_pr(session)
    if not pr_info:
        logger.warning(f"Task {fp} session completed but no PR found")
        return

    # Update ACUs and transition to VERIFYING
    acus_consumed = session.get("acus_consumed", 0)
    new_acus_total = task.get("acus_total", 0) + acus_consumed

    # Store structured_output in attempts table
    attempt_id = task.get("current_attempt_id")  # This should be set when session created
    if attempt_id:
        db.finish_attempt(
            attempt_id,
            session_id=session.get("session_id"),
            session_url=session.get("url"),
            outcome="completed",
            claim_json=pr_info.get("structured_output"),
            acus_consumed=acus_consumed,
            ended_at=datetime.utcnow().isoformat(),
        )

    if db.transition(fp, from_state, db.State.VERIFYING, acus_total=new_acus_total):
        logger.info(f"Task {fp} -> VERIFYING (session completed)")


async def tick() -> None:
    """Run one tick of the reconciler.
    
    Each tick:
    1. Fetches open issues labeled cognition-project:auto (desired state)
    2. Fetches all tagged sessions (observed state)
    3. Joins them to the ledger by fingerprint and reconciles
    4. Takes at most one transition per task
    """
    global _halt_latched, _last_tick_time, _last_tick_exception, _orphaned_session_count
    
    _last_tick_time = datetime.utcnow()
    _last_tick_exception = None
    _orphaned_session_count = 0
    
    github_client = github.get_client()
    devin_client = devin.get_client()
    
    # Fetch desired state: open issues labeled cognition-project:auto
    try:
        open_issues = github_client.list_open_issues(label="cognition-project:auto")
    except Exception as e:
        logger.error(f"Failed to fetch open issues: {e}")
        _last_tick_exception = str(e)
        return
    
    # Fetch observed state: all tagged sessions
    try:
        tagged_sessions = devin_client.list_tagged_sessions()
    except Exception as e:
        logger.error(f"Failed to fetch tagged sessions: {e}")
        _last_tick_exception = str(e)
        return
    
    # Build session lookup by fingerprint
    # Session title format: [cognition-project] scan:<fp> att=<attempt_no> — <description>
    session_by_fp: Dict[str, Dict[str, Any]] = {}
    unrecognized_sessions: List[Dict[str, Any]] = []
    
    for session in tagged_sessions:
        title = session.get("title", "")
        # Extract fingerprint from title
        if "[cognition-project]" in title and "scan:" in title:
            try:
                # Parse: [cognition-project] scan:a91c3f2e att=1 — B608 sqla/models.py
                parts = title.split()
                fp_part = [p for p in parts if p.startswith("scan:")]
                if fp_part:
                    fp = fp_part[0]  # e.g., "scan:a91c3f2e"
                    session_by_fp[fp] = session
            except Exception as e:
                logger.warning(f"Failed to parse session title '{title}': {e}")
                unrecognized_sessions.append(session)
        else:
            unrecognized_sessions.append(session)
    
    # Log unrecognized sessions (should surface on status page)
    if unrecognized_sessions:
        _orphaned_session_count = len(unrecognized_sessions)
        logger.warning(f"Found {len(unrecognized_sessions)} unrecognized tagged sessions")
        for session in unrecognized_sessions:
            logger.warning(f"  Unrecognized session: {session.get('title', 'unknown')} (id: {session.get('session_id')})")
    
    # Build task lookup by fingerprint
    tasks = db.list_tasks()
    task_by_fp: Dict[str, Dict[str, Any]] = {task["fp"]: task for task in tasks}
    
    # Join issues to tasks by fingerprint (from issue body footer)
    issue_by_fp: Dict[str, Dict[str, Any]] = {}
    for issue in open_issues:
        body = issue.get("body", "")
        # Extract fingerprint from footer: <!-- cognition-project:fp=<fp> -->
        if "<!-- cognition-project:fp=" in body:
            try:
                start = body.index("<!-- cognition-project:fp=") + len("<!-- cognition-project:fp=")
                end = body.index("-->", start)
                fp = body[start:end].strip()
                issue_by_fp[fp] = issue
            except (ValueError, IndexError):
                logger.warning(f"Failed to parse fingerprint from issue #{issue.get('number')}")
    
    # Reconcile each task
    for fp, task in task_by_fp.items():
        try:
            await reconcile_task(fp, task, session_by_fp.get(fp), issue_by_fp.get(fp), github_client, devin_client)
        except Exception as e:
            logger.error(f"Error reconciling task {fp}: {e}")
            _last_tick_exception = str(e)


async def reconcile_task(
    fp: str,
    task: Dict[str, Any],
    session: Optional[Dict[str, Any]],
    issue: Optional[Dict[str, Any]],
    github_client,
    devin_client
) -> None:
    """Reconcile a single task, taking at most one transition.
    
    Args:
        fp: Task fingerprint.
        task: Task dictionary from the database.
        session: Session dictionary from Devin API (if exists).
        issue: Issue dictionary from GitHub API (if exists).
        github_client: GitHub client instance.
        devin_client: Devin client instance.
    """
    state = task.get("state", db.State.PENDING)
    attempt_count = task.get("attempt_count", 0)
    
    # PENDING -> RUNNING: create session if policy admits
    if state == db.State.PENDING:
        admitted, reason = can_admit(task)
        if admitted:
            await create_session(fp, task, devin_client)
        else:
            logger.info(f"Task {fp} not admitted: {reason}")
    
    # RUNNING: normalize and transition based on session state
    elif state == db.State.RUNNING:
        if not session:
            # Fallback: try to get session directly by ID if insights list was incomplete
            current_attempt = db.get_current_attempt(fp)
            if current_attempt and current_attempt.get("session_id"):
                session_id = current_attempt.get("session_id")
                logger.info(f"Task {fp} not in insights list, fetching session {session_id} directly")
                session = devin_client.get_session(session_id)
                if not session:
                    logger.warning(f"Task {fp} is RUNNING but session {session_id} not found")
                    return
            else:
                logger.warning(f"Task {fp} is RUNNING but has no session")
                return
        
        normalized = devin.normalize(session.get("status"), session.get("status_detail"))
        
        # Check wall clock. Measured from when the attempt started, not from
        # task.updated_at - that field moves on any DB write, so it counts
        # reconciler downtime against the agent and kills work on restart.
        attempt = db.get_current_attempt(fp)
        started_at = (attempt or {}).get("started_at") or task.get("updated_at")
        wall_clock_minutes = (datetime.utcnow() - datetime.fromisoformat(started_at)).total_seconds() / 60
        if wall_clock_minutes > config.WALL_CLOCK_MINUTES:
            logger.warning(f"Task {fp} wall clock exceeded ({wall_clock_minutes:.1f}m > {config.WALL_CLOCK_MINUTES}m)")
            if db.transition(fp, db.State.RUNNING, db.State.FAILED):
                logger.info(f"Task {fp} -> FAILED (wall clock exceeded)")
            return
        
        # RUNNING -> BLOCKED: agent asked a question
        if normalized == devin.Internal.BLOCKED:
            if db.transition(fp, db.State.RUNNING, db.State.BLOCKED):
                logger.info(f"Task {fp} -> BLOCKED (agent asked question)")
                # Comment on issue with agent's question and session link
                if issue:
                    question = session.get("status_detail", "Agent is waiting for user input")
                    session_url = session.get("url", "")
                    comment = f"Agent has a question:\n\n{question}\n\nSession: {session_url}"
                    github_client.comment(issue.get("number"), comment)
        
        # RUNNING -> VERIFYING: session completed, harvest PR
        elif normalized == devin.Internal.DONE:
            move_to_verifying(fp, task, session, db.State.RUNNING)
        
        # RUNNING -> FAILED: session failed
        elif normalized == devin.Internal.FAILED:
            if db.transition(fp, db.State.RUNNING, db.State.FAILED):
                logger.info(f"Task {fp} -> FAILED (session failed)")
        
        # RUNNING -> HALT: HALT signal from platform
        elif normalized == devin.Internal.HALT:
            global _halt_latched
            _halt_latched = True
            logger.error(f"HALT signal received from platform for task {fp}")
            if db.transition(fp, db.State.RUNNING, db.State.FAILED):
                logger.info(f"Task {fp} -> FAILED (HALT signal)")
    
    # VERIFYING: run the verifier
    elif state == db.State.VERIFYING:
        await run_verification(fp, task, session, issue, github_client, devin_client)
    
    # READY: check if PR is merged
    elif state == db.State.READY:
        if not session:
            logger.warning(f"Task {fp} is READY but has no session")
            return
        
        pr_info = harvest_pr(session)
        if pr_info and pr_info.get("pr_state") == "merged":
            if db.transition(fp, db.State.READY, db.State.MERGED):
                logger.info(f"Task {fp} -> MERGED")
                if issue:
                    github_client.close_issue(issue.get("number"))
    
    # BLOCKED: can be resumed if session becomes RUNNING again, or the PR may have
    # already been merged/acted on directly on GitHub while the session sat waiting
    elif state == db.State.BLOCKED:
        if session:
            if harvest_pr(session):
                move_to_verifying(fp, task, session, db.State.BLOCKED)
            else:
                normalized = devin.normalize(session.get("status"), session.get("status_detail"))
                if normalized == Internal.RUNNING:
                    if db.transition(fp, db.State.BLOCKED, db.State.RUNNING):
                        logger.info(f"Task {fp} -> RUNNING (resumed)")


async def create_session(fp: str, task: Dict[str, Any], devin_client) -> None:
    """Create a Devin session for a task.
    
    Args:
        fp: Task fingerprint.
        task: Task dictionary from the database.
        devin_client: Devin client instance.
    """
    attempt_no = task.get("attempt_count", 0) + 1
    issue_number = task.get("issue_number")
    
    # Generate request ID for idempotency
    request_id = str(uuid.uuid4())
    
    # Write attempt row BEFORE create call (idempotency)
    attempt_id = db.start_attempt(fp, attempt_no, request_id)
    
    # Update task with current attempt ID
    db.transition(fp, db.State.PENDING, db.State.PENDING, current_attempt_id=attempt_id)
    
    # Render brief
    brief = prompts.render_brief(task, attempt_no)
    
    # Build session title
    payload = task.get("payload_json", {})
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)
    
    rule_id = payload.get("test_id", "unknown")
    file_path = payload.get("file_path", "unknown")
    file_name = Path(file_path).name
    title = f"[cognition-project] {fp} att={attempt_no} — {rule_id} {file_name}"
    
    # Create session
    try:
        response = devin_client.create_session(
            prompt=brief,
            title=title,
            schema=prompts.structured_output_schema()
        )
        
        # Update attempt with session info
        db.finish_attempt(
            attempt_id,
            session_id=response.get("session_id"),
            session_url=response.get("url"),
        )
        
        # Transition to RUNNING
        if db.transition(fp, db.State.PENDING, db.State.RUNNING, attempt_count=attempt_no):
            logger.info(f"Task {fp} -> RUNNING (session created: {response.get('session_id')})")
    
    except Exception as e:
        logger.error(f"Failed to create session for task {fp}: {e}")
        # Mark attempt as failed
        db.finish_attempt(attempt_id, outcome="failed", ended_at=datetime.utcnow().isoformat())


async def run_verification(fp: str, task: Dict[str, Any], session: Optional[Dict[str, Any]], issue: Optional[Dict[str, Any]], github_client, devin_client) -> None:
    """Run verification for a task.

    Args:
        fp: Task fingerprint.
        task: Task dictionary from the database.
        session: Session dictionary from Devin API.
        issue: Issue dictionary from GitHub API.
        github_client: GitHub client instance.
        devin_client: Devin client instance.
    """
    if not session:
        logger.warning(f"Task {fp} is VERIFYING but has no session")
        return
    
    pr_info = harvest_pr(session)
    if not pr_info:
        logger.warning(f"Task {fp} is VERIFYING but has no PR")
        return
    
    # Get PR details
    pr_url = pr_info.get("pr_url")
    if not pr_url:
        logger.warning(f"Task {fp} has no PR URL")
        return
    
    # Extract PR number from URL
    try:
        pr_number = int(pr_url.split("/")[-1])
    except (ValueError, IndexError):
        logger.error(f"Failed to parse PR number from URL: {pr_url}")
        return
    
    # Get PR details from GitHub
    try:
        pr = github_client.find_pr_for_branch(scanner.branch_name(fp))
        if not pr:
            logger.warning(f"PR not found for task {fp}")
            return
    except Exception as e:
        logger.error(f"Failed to fetch PR for task {fp}: {e}")
        return
    
    # Get diff and changed files
    try:
        diff = github_client.get_pr_diff(pr_number)
        changed_files = github_client.get_pr_files(pr_number)
    except Exception as e:
        logger.error(f"Failed to fetch PR diff for task {fp}: {e}")
        return
    
    # Build verification context
    payload = task.get("payload_json", {})
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)
    
    # Get known fingerprints for the same rule only - comparing across rules
    # causes false positives when the scanner filters by RULE_ALLOWLIST
    current_rule_id = payload.get("test_id", "unknown")
    all_tasks = db.list_tasks()
    same_rule_fingerprints = []
    for t in all_tasks:
        t_payload = t.get("payload_json", {})
        if isinstance(t_payload, str):
            t_payload = json.loads(t_payload)
        if t_payload.get("test_id") == current_rule_id:
            same_rule_fingerprints.append(t["fp"])
    
    ctx = verifier.VerifyContext(
        fp=fp,
        rule_id=current_rule_id,
        issue_number=task.get("issue_number"),
        branch=scanner.branch_name(fp),
        pr_number=pr_number,
        pr_head_sha=pr.get("head_sha", ""),
        pr_body=pr.get("body", ""),
        changed_files=changed_files,
        diff=diff,
        repo_path=config.TARGET_REPO_PATH,
        known_fingerprints=same_rule_fingerprints,
    )
    
    # Run verifier
    try:
        verdict = verifier.verify(ctx)
    except Exception as e:
        logger.error(f"Verification failed for task {fp}: {e}")
        return
    
    # Update attempt with verdict
    attempt_id = task.get("current_attempt_id")
    if attempt_id:
        db.finish_attempt(
            attempt_id,
            verdict_passed=1 if verdict.passed else 0,
            gate_failed=verdict.gate,
            evidence_json=verdict.evidence,
            ended_at=datetime.utcnow().isoformat(),
        )
    
    # Handle verdict
    outcome = decide_outcome(verdict, task.get("attempt_count", 0))

    if outcome == OUTCOME_WAIT:
        logger.error(
            f"Task {fp} verification could not complete (gate={verdict.gate}): "
            f"{verdict.reason}. Staying in VERIFYING; will retry next tick."
        )
        return

    if outcome == OUTCOME_READY:
        # Pass: label and post evidence
        if db.transition(fp, db.State.VERIFYING, db.State.READY):
            logger.info(f"Task {fp} -> READY (verification passed)")
            
            if issue:
                # Add labels
                github_client.add_labels(issue.get("number"), ["cognition-project:verified", "ready-for-review"])
                
                # Post evidence comment
                evidence_comment = f"""## Verification passed

**Verdict:** {verdict.reason}

**Evidence:**
- Diff stats: {verdict.evidence.get('diff_added_lines', 0)}+ {verdict.evidence.get('diff_removed_lines', 0)}-
- Session: {session.get('url')}
- ACUs consumed: {session.get('acus_consumed', 0)}

**Gates passed:** {', '.join(verdict.evidence.get('gates_passed', []))}
{_skipped_gates_note(verdict)}"""
                github_client.comment(issue.get("number"), evidence_comment)

    elif outcome == OUTCOME_RETRY:
        # Retry: message the same session
        attempt_count = task.get("attempt_count", 0)
        logger.info(f"Task {fp} retrying (attempt {attempt_count + 1}/{config.MAX_ATTEMPTS})")

        if db.transition(fp, db.State.VERIFYING, db.State.RUNNING):
            db.increment_attempt_count(fp)

            # Send message to session with verbatim rejection
            devin_client.send_message(
                session.get("session_id"),
                f"Verification failed:\n\n{verdict.reason}"
            )

    else:
        # Quarantine
        logger.warning(f"Task {fp} -> QUARANTINED (max attempts reached)")

        if db.transition(fp, db.State.VERIFYING, db.State.QUARANTINED):
            if issue:
                # Label needs-human
                github_client.add_labels(issue.get("number"), ["needs-human"])

                # Post quarantine comment with diffs and rejections
                quarantine_comment = f"""## Task quarantined

This task has reached the maximum number of attempts ({config.MAX_ATTEMPTS}) without passing verification.

**Final rejection:** {verdict.reason}

**Gates failed:** {verdict.gate or 'unknown'}

The branch and PR are left open for forensic analysis.
"""
                github_client.comment(issue.get("number"), quarantine_comment)


async def run_loop() -> None:
    """Run the main reconciler loop."""
    logger.info("Starting reconciler loop")
    
    while True:
        try:
            await tick()
        except Exception as e:
            logger.error(f"Error in tick: {e}")
        
        # Wait for next tick
        await asyncio.sleep(config.TICK_SECONDS)


async def run_once(dry_run: bool = False) -> None:
    """Run a single tick for testing.
    
    Args:
        dry_run: If True, stub all network calls and print transitions.
    """
    if dry_run:
        logger.info("Running single tick in DRY_RUN mode")
        
        # Get all tasks from database
        tasks = db.list_tasks()
        
        logger.info(f"Tasks in database: {len(tasks)}")
        for task in tasks:
            fp = task["fp"]
            state = task["state"]
            logger.info(f"  Task {fp}: {state}")
            
            # Print what transition would be taken
            if state == db.State.PENDING:
                admitted, reason = can_admit(task)
                if admitted:
                    logger.info(f"    Would transition: PENDING -> RUNNING (create session)")
                else:
                    logger.info(f"    Would not admit: {reason}")
            elif state == db.State.RUNNING:
                logger.info(f"    Would check session status and normalize")
            elif state == db.State.VERIFYING:
                logger.info(f"    Would run verification gates")
            elif state == db.State.READY:
                logger.info(f"    Would check if PR is merged")
            else:
                logger.info(f"    No transition for state {state}")
    else:
        await tick()


if __name__ == "__main__":
    import argparse
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    parser = argparse.ArgumentParser(description="Cognition-project reconciler")
    parser.add_argument("--once", action="store_true", help="Run a single tick instead of loop")
    parser.add_argument("--dry-run", action="store_true", help="Stub all network calls")
    
    args = parser.parse_args()
    
    # Initialize database
    db.init_db()
    
    if args.once:
        asyncio.run(run_once(dry_run=args.dry_run))
    else:
        asyncio.run(run_loop())
