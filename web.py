"""Web interface for cognition-project.

FastAPI with three routes:
- POST /webhook — dumb webhook that triggers immediate reconciler tick
- GET /status — server-rendered HTML status page, no JavaScript
- POST /scan — manual scan trigger for demo purposes
"""

import sqlite3
import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

import config
import db
from scanner import scan, sync_findings

app = FastAPI(title="cognition-project")


# Global flag for immediate tick
_webhook_received = False


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    """Webhook endpoint for GitHub events.
    
    This is deliberately dumb — it does not parse the payload, verify a signature,
    or deduplicate deliveries. It simply sets a flag that makes the next reconciler
    tick run immediately.
    
    The loop is already idempotent, so the webhook is allowed to be dumb: it makes
    the system faster, not more correct. If it breaks entirely, the 30-second tick
    catches everything anyway.
    """
    global _webhook_received
    _webhook_received = True
    return Response(status_code=200)


@app.get("/status", response_class=HTMLResponse)
async def status() -> str:
    """Server-rendered HTML status page.
    
    No JavaScript, no build step. Reads straight from SQLite.
    Shows counts, not rates — rates are fiction at a dozen tasks.
    """
    # Get database path
    db_path = config.DB_PATH
    
    # Check if database exists
    if not os.path.exists(db_path):
        return """
        <html>
        <head><title>COGNITION-PROJECT</title></head>
        <body>
        <h1>COGNITION-PROJECT</h1>
        <p>No database found. Run a scan first.</p>
        </body>
        </html>
        """
    
    # Query database for counts
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        
        # Count by state
        cursor = conn.execute("""
            SELECT state, COUNT(*) as count
            FROM tasks
            GROUP BY state
        """)
        state_counts = {row["state"]: row["count"] for row in cursor.fetchall()}
        
        # Get all tasks with their attempts
        cursor = conn.execute("""
            SELECT t.*, a.session_id, a.session_url, a.verdict_passed, a.gate_failed,
                   a.claim_json, a.evidence_json, a.acus_consumed
            FROM tasks t
            LEFT JOIN attempts a ON t.current_attempt_id = a.id
            ORDER BY t.updated_at DESC
        """)
        tasks = [dict(row) for row in cursor.fetchall()]
        
        # Get today's ACU usage
        cursor = conn.execute("""
            SELECT SUM(acus_total) as total
            FROM tasks
            WHERE date(updated_at) = date('now')
        """)
        acu_result = cursor.fetchone()
        acus_today = acu_result["total"] if acu_result and acu_result["total"] else 0.0
    
    # Build counts
    total_findings = state_counts.get("PENDING", 0) + state_counts.get("RUNNING", 0) + \
                     state_counts.get("VERIFYING", 0) + state_counts.get("READY", 0) + \
                     state_counts.get("MERGED", 0) + state_counts.get("QUARANTINED", 0) + \
                     state_counts.get("FAILED", 0)
    
    verified = state_counts.get("READY", 0) + state_counts.get("MERGED", 0)
    running = state_counts.get("RUNNING", 0) + state_counts.get("VERIFYING", 0)
    needs_human = state_counts.get("BLOCKED", 0) + state_counts.get("QUARANTINED", 0)
    quarantined = state_counts.get("QUARANTINED", 0)
    
    # Check HALT latch (import reconciler module to access the global)
    halt_banner = ""
    try:
        import reconciler
        if reconciler._halt_latched:
            halt_banner = '<div style="background-color: #ffcccc; padding: 10px; margin-bottom: 10px; border: 1px solid #ff0000;"><strong>HALT LATCHED — no new sessions allowed</strong></div>'
    except ImportError:
        pass  # reconciler module not available in demo mode
    
    # Build HTML
    html = f"""
    <html>
    <head>
        <title>COGNITION-PROJECT</title>
        <style>
            body {{ font-family: monospace; max-width: 1200px; margin: 20px auto; padding: 0 20px; }}
            h1 {{ margin-bottom: 10px; }}
            .header {{ border-bottom: 1px solid #ccc; padding-bottom: 10px; margin-bottom: 20px; }}
            .counts {{ margin-bottom: 20px; }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
            .state-PENDING {{ background-color: #fff3cd; }}
            .state-RUNNING {{ background-color: #d1ecf1; }}
            .state-VERIFYING {{ background-color: #d1ecf1; }}
            .state-READY {{ background-color: #d4edda; }}
            .state-MERGED {{ background-color: #d4edda; }}
            .state-QUARANTINED {{ background-color: #f8d7da; }}
            .state-FAILED {{ background-color: #f8d7da; }}
            .state-BLOCKED {{ background-color: #fff3cd; }}
            .claim-verdict {{ font-weight: bold; color: #333; }}
        </style>
    </head>
    <body>
        <h1>COGNITION-PROJECT</h1>
        <div class="header">
            today: {acus_today:.1f} / {config.DAILY_ACU_CEILING} ACU
        </div>
        
        {halt_banner}
        
        <div class="counts">
            {total_findings} findings   {verified} verified   {running} running   {needs_human} needs-human   {quarantined} quarantined
        </div>
        
        <table>
            <thead>
                <tr>
                    <th>fp</th>
                    <th>state</th>
                    <th>att</th>
                    <th>ACU</th>
                    <th>claim → verdict</th>
                    <th>links</th>
                </tr>
            </thead>
            <tbody>
    """
    
    # Add task rows
    for task in tasks:
        fp = task["fp"]
        state = task["state"]
        attempt_count = task["attempt_count"]
        acus = task["acus_total"]
        
        # Build claim → verdict column
        claim_verdict = build_claim_verdict(task)
        
        # Build links
        issue_number = task.get("issue_number")
        session_url = task.get("session_url")
        
        links = []
        if issue_number:
            links.append(f'<a href="https://github.com/{config.GITHUB_REPO}/issues/{issue_number}">issue</a>')
        if session_url:
            links.append(f'<a href="{session_url}">session</a>')
        
        links_str = "·".join(links) if links else ""
        
        html += f"""
                <tr class="state-{state}">
                    <td>{fp}</td>
                    <td>{state}</td>
                    <td>{attempt_count}</td>
                    <td>{acus:.1f}</td>
                    <td class="claim-verdict">{claim_verdict}</td>
                    <td>{links_str}</td>
                </tr>
        """
    
    html += """
            </tbody>
        </table>
        
        <div style="margin-top: 20px;">
            <form action="/scan" method="post">
                <button type="submit">Scan now</button>
            </form>
        </div>
    </body>
    </html>
    """
    
    return html


def build_claim_verdict(task: dict) -> str:
    """Build the claim → verdict column for a task.
    
    Walks the task's attempts in order, showing the agent's claim followed by each verdict.
    This is the whole point of the project rendered as data: per task, what trusting the
    agent's self-report would have cost.
    """
    fp = task["fp"]
    db_path = config.DB_PATH
    
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
            SELECT attempt_no, claim_json, verdict_passed, gate_failed
            FROM attempts
            WHERE fp = ?
            ORDER BY attempt_no
        """, (fp,))
        attempts = [dict(row) for row in cursor.fetchall()]
    
    if not attempts:
        return "—"
    
    parts = []
    for attempt in attempts:
        import json
        
        # Parse claim
        claim_json = attempt.get("claim_json")
        if claim_json:
            try:
                if isinstance(claim_json, str):
                    claim = json.loads(claim_json)
                else:
                    claim = claim_json
                
                # Extract the claim summary
                if isinstance(claim, dict):
                    claim_text = claim.get("summary", claim.get("fix_description", "fixed"))
                else:
                    claim_text = str(claim)
            except (json.JSONDecodeError, TypeError):
                claim_text = "fixed"
        else:
            claim_text = "fixed"
        
        # Add verdict
        verdict_passed = attempt.get("verdict_passed")
        gate_failed = attempt.get("gate_failed")
        
        if verdict_passed == 1:
            verdict = "PASS"
        elif gate_failed:
            verdict = f"REJECT ({gate_failed})"
        else:
            verdict = "REJECT"
        
        parts.append(f"{claim_text} → {verdict}")
    
    return " → ".join(parts)


@app.post("/scan")
async def trigger_scan() -> Response:
    """Trigger a scan for demo purposes.
    
    This allows the demo to work without waiting for a webhook to arrive on cue.
    """
    try:
        # Run scan against current directory
        findings = scan(".")
        
        # Sync findings to database
        sync_findings(findings)
        
        return Response(content=f"Scan complete. {len(findings)} findings synced.", status_code=200)
    except Exception as e:
        return Response(content=f"Scan failed: {str(e)}", status_code=500)


def should_tick_immediately() -> bool:
    """Check if a webhook was received and reset the flag."""
    global _webhook_received
    if _webhook_received:
        _webhook_received = False
        return True
    return False