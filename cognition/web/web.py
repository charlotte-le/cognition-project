"""Web interface for cognition-project.

FastAPI with four routes:
- POST /webhook — dumb webhook that triggers immediate reconciler tick
- GET /status — server-rendered HTML status page, no JavaScript
- POST /scan — manual scan trigger for demo purposes
- GET /metrics.json — programmatic access to metrics (parseable JSON)
"""

import sqlite3
import os
import json
from datetime import datetime, timezone
from html import escape
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from cognition.core import config, db
from cognition.verification import scanner

app = FastAPI(title="cognition-project")


# Global flag for immediate tick
_webhook_received = False


# Roughly three lines of claim text at the Claim → Verdict column width and type
# size. Past this the claim gets a Show more toggle; under it, it already fits.
CLAIM_CLAMP_CHARS = 165


def _muted(value, muted: bool = True) -> str:
    """Grey out a value so the eye skips it.

    Most of this page is zeros and em-dashes; muting them is what makes the one
    row that is actually doing something visible at a glance.
    """
    text = escape(str(value))
    return f'<span class="zero">{text}</span>' if muted else text


def _count(n: int, label: str) -> str:
    """Render `N label`, bold when it is non-zero and muted when it is not.

    Only the numeral changes weight — the label is permanent page furniture and
    stays at full contrast. Muting whole phrases dims the page instead of
    ranking it, since on a quiet day nearly every count is zero.
    """
    if n:
        return f"<strong>{n}</strong> {escape(label)}"
    return f'<span class="zero">{n}</span> {escape(label)}'


def _stat(value: str, label: str, is_zero: bool = False) -> str:
    """One tile in the headline stat strip: big value over a small caps label."""
    cls = "stat is-zero" if is_zero else "stat"
    return (
        f'<div class="{cls}">'
        f'<div class="stat-value">{escape(str(value))}</div>'
        f'<div class="stat-label">{escape(label)}</div>'
        f"</div>"
    )


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
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="color-scheme" content="light">
            <title>COGNITION-PROJECT</title>
            <style>
                body { font-family: monospace; max-width: 1200px; margin: 20px auto;
                       padding: 0 20px; background-color: #fff; color: #111; }
            </style>
        </head>
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
        
        # Get today's ACU usage - use attempts.ended_at for accurate attribution
        cursor = conn.execute("""
            SELECT SUM(acus_consumed) as total
            FROM attempts
            WHERE date(ended_at) = date('now')
        """)
        acu_result = cursor.fetchone()
        acus_today = acu_result["total"] if acu_result and acu_result["total"] else 0.0
        
        # TRUST: agent claims vs verifier confirmations
        cursor = conn.execute("""
            SELECT 
                COUNT(CASE WHEN verdict_passed = 1 THEN 1 END) as confirmed,
                COUNT(CASE WHEN verdict_passed = 0 THEN 1 END) as caught,
                COUNT(*) as total_claims
            FROM attempts
            WHERE verdict_passed IS NOT NULL
        """)
        trust_result = cursor.fetchone()
        agent_claims = trust_result["total_claims"] if trust_result and trust_result["total_claims"] else 0
        verifier_confirmed = trust_result["confirmed"] if trust_result and trust_result["confirmed"] else 0
        verifier_caught = trust_result["caught"] if trust_result and trust_result["caught"] else 0
        trust_rate_str = f"{(verifier_confirmed / agent_claims * 100):.0f}%" if agent_claims else "—"

        # COST: ROI calculation
        cursor = conn.execute("""
            SELECT 
                SUM(a.acus_consumed) as total_acus
            FROM attempts a
            WHERE a.ended_at IS NOT NULL
        """)
        cost_result = cursor.fetchone()
        total_acus_all = cost_result["total_acus"] if cost_result and cost_result["total_acus"] else 0.0
        
        # Count merged PRs separately
        cursor = conn.execute("""
            SELECT COUNT(*) as merged_prs
            FROM tasks
            WHERE state = 'MERGED'
        """)
        merged_result = cursor.fetchone()
        merged_prs = merged_result["merged_prs"] if merged_result and merged_result["merged_prs"] else 0
        
        # GATES: rejections by gate
        cursor = conn.execute("""
            SELECT gate_failed, COUNT(*) as count
            FROM attempts
            WHERE gate_failed IS NOT NULL AND gate_failed != ''
            GROUP BY gate_failed
        """)
        gate_results = cursor.fetchall()
        gate_failures = {row["gate_failed"]: row["count"] for row in gate_results}
        
        # Median cycle time (find→verified)
        cursor = conn.execute("""
            SELECT 
                AVG((julianday(a.ended_at) - julianday(t.created_at)) * 24 * 60) as avg_minutes
            FROM attempts a
            JOIN tasks t ON a.fp = t.fp
            WHERE a.verdict_passed = 1 AND a.ended_at IS NOT NULL
        """)
        cycle_time_result = cursor.fetchone()
        avg_cycle_time = cycle_time_result["avg_minutes"] if cycle_time_result and cycle_time_result["avg_minutes"] else None
    
    # Build counts
    total_findings = state_counts.get("PENDING", 0) + state_counts.get("RUNNING", 0) + \
                     state_counts.get("VERIFYING", 0) + state_counts.get("READY", 0) + \
                     state_counts.get("MERGED", 0) + state_counts.get("QUARANTINED", 0) + \
                     state_counts.get("FAILED", 0) + state_counts.get("BLOCKED", 0)
    
    pending = state_counts.get("PENDING", 0)
    running = state_counts.get("RUNNING", 0) + state_counts.get("VERIFYING", 0)
    verified = state_counts.get("READY", 0)
    merged = state_counts.get("MERGED", 0)
    quarantined = state_counts.get("QUARANTINED", 0)
    needs_human = state_counts.get("BLOCKED", 0)
    failed = state_counts.get("FAILED", 0)
    needs_attention = needs_human + quarantined + failed

    # Sort tasks by what needs a human first, not just recency.
    # BLOCKED/QUARANTINED/FAILED need action now; READY is waiting on a merge;
    # RUNNING/VERIFYING/PENDING are in flight; MERGED is done.
    state_priority = {
        "BLOCKED": 0, "QUARANTINED": 1, "FAILED": 2, "READY": 3,
        "RUNNING": 4, "VERIFYING": 4, "PENDING": 5, "MERGED": 6,
    }
    tasks.sort(key=lambda t: state_priority.get(t["state"], 99))

    # Check HALT latch and liveness (import reconciler module to access the globals)
    halt_banner = ""
    last_tick_str = "never"
    exception_banner = ""
    try:
        import reconciler
        if reconciler._halt_latched:
            halt_banner = '<div class="banner banner-stop"><strong>HALT LATCHED</strong> — no new sessions allowed</div>'
        
        # Get last tick time
        last_tick_time = reconciler.get_last_tick_time()
        if last_tick_time:
            now = datetime.now(timezone.utc)
            # Ensure both datetimes are timezone-aware
            if last_tick_time.tzinfo is None:
                last_tick_time = last_tick_time.replace(tzinfo=timezone.utc)
            seconds_ago = (now - last_tick_time).total_seconds()
            if seconds_ago < 60:
                last_tick_str = f"{int(seconds_ago)}s ago"
            else:
                minutes_ago = int(seconds_ago / 60)
                last_tick_str = f"{minutes_ago}m ago"
        
        # Get last tick exception
        last_exception = reconciler.get_last_tick_exception()
        if last_exception:
            exception_banner = f'<div class="banner banner-stop"><strong>Last tick error:</strong> {escape(str(last_exception))}</div>'

        # Get orphaned session count
        orphaned_count = reconciler.get_orphaned_session_count()
        if orphaned_count > 0:
            orphaned_banner = f'<div class="banner banner-warn"><strong>{orphaned_count} orphaned session(s)</strong> detected</div>'
            exception_banner = exception_banner + orphaned_banner if exception_banner else orphaned_banner
    except ImportError:
        pass  # reconciler module not available in demo mode
    
    # Render the gate summary — escaped, since gate names come from attempt rows.
    gates_str = " · ".join(
        f"{escape(str(gate))} {count}" for gate, count in gate_failures.items()
    ) if gate_failures else '<span class="zero">no rejections</span>'

    # Build HTML.
    #
    # Case carries meaning here, so it is applied consistently:
    #   ALL CAPS    — machine enum values (PENDING, RUNNING, HALT LATCHED)
    #   Title Case  — labels we wrote (column headers, buttons)
    #   lowercase   — prose and values (15s ago, 0.0 / 25.0 ACU)
    # The demoted small labels (TRUST/COST/GATES, column headers) are a separate
    # register from body-size caps, so they don't compete with the state values.
    html = f"""
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="color-scheme" content="light">
        <meta http-equiv="refresh" content="30">
        <title>COGNITION-PROJECT</title>
        <style>
            :root {{
                /* Sans carries the chrome — labels, headers, prose. Mono carries the
                   data — fingerprints, counts, states. Mixing them is what makes a
                   dense table scannable; monospace everywhere flattens the hierarchy. */
                --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, system-ui, sans-serif;
                --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
                --ink: #1a1a1a;
                --ink-soft: #5c5c5c;
                --ink-mute: #767676;   /* 4.5:1 on white */
                --rule: #e6e6e6;
            }}
            body {{ font-family: var(--sans); font-size: 14px; line-height: 1.5;
                   background-color: #fff; color: var(--ink);
                   max-width: 1200px; margin: 24px auto; padding: 0 24px;
                   -webkit-font-smoothing: antialiased; }}
            a {{ color: #0b5cad; text-decoration: underline; text-underline-offset: 2px; }}
            a:hover {{ color: #083f76; }}
            .zero {{ color: var(--ink-mute); }}

            /* Small caps labels — one shared register for every label on the page. */
            .stat-label, .metric-label, .section, thead th {{
                font-family: var(--sans); font-size: 11px; font-weight: 700;
                letter-spacing: 0.08em; text-transform: uppercase; }}

            .header {{ display: flex; align-items: flex-start; justify-content: space-between;
                      gap: 24px; border-bottom: 2px solid var(--ink);
                      padding-bottom: 16px; margin-bottom: 24px; }}
            h1 {{ margin: 0 0 4px; font-size: 20px; font-weight: 700; letter-spacing: 0.04em; }}
            .subhead {{ font-family: var(--mono); font-size: 12.5px; color: var(--ink-soft); }}
            button {{ font-family: var(--sans); font-size: 13px; font-weight: 600;
                     background-color: var(--ink); color: #fff; border: 1px solid var(--ink);
                     border-radius: 6px; padding: 8px 16px; cursor: pointer; white-space: nowrap; }}
            button:hover {{ background-color: #000; }}

            .banner {{ padding: 12px 14px; margin-bottom: 14px; border-radius: 6px; }}
            .banner-stop {{ background-color: #ffe3e3; border: 1px solid #e05252; }}
            .banner-warn {{ background-color: #fff3cd; border: 1px solid #d99b28; }}

            .stats {{ display: flex; flex-wrap: wrap; gap: 40px; margin-bottom: 6px; }}
            .stat-value {{ font-family: var(--mono); font-size: 26px; font-weight: 700; line-height: 1.15; }}
            .stat-label {{ color: var(--ink-mute); margin-top: 3px; }}
            .stat.is-zero .stat-value {{ color: var(--ink-mute); font-weight: 400; }}
            .stats-detail {{ font-family: var(--mono); font-size: 12.5px;
                            color: var(--ink-soft); margin-bottom: 26px; }}

            .metrics {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 28px;
                       padding: 18px 20px; margin-bottom: 30px;
                       background-color: #fafafa; border: 1px solid var(--rule); border-radius: 8px; }}
            .metric-label {{ color: var(--ink-mute); margin-bottom: 5px; }}
            .metric-value {{ font-family: var(--mono); font-size: 17px; font-weight: 700; }}
            .metric-detail {{ font-family: var(--mono); font-size: 12.5px;
                             color: var(--ink-soft); margin-top: 2px; }}

            .section {{ color: var(--ink-mute); margin-bottom: 10px; }}
            table {{ border-collapse: collapse; width: 100%; }}
            thead th {{ position: sticky; top: 0; background-color: #fff; z-index: 1;
                       color: #333; text-align: left; white-space: nowrap;
                       padding: 0 12px 9px; box-shadow: inset 0 -2px 0 var(--ink); }}
            tbody td {{ font-family: var(--mono); font-size: 13px; padding: 11px 12px;
                       border-bottom: 1px solid var(--rule); vertical-align: top; }}
            tbody tr:hover {{ background-color: #fafafa; }}
            td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
            /* Claim → Verdict: verdict always visible, claim clamped to 3 lines. */
            td.claim-verdict {{ color: #333; max-width: 620px; }}
            .attempt {{ position: relative; margin-bottom: 10px; }}
            .attempt:last-child {{ margin-bottom: 0; }}
            .verdict {{ display: inline-block; font-family: var(--mono); font-size: 11px;
                       font-weight: 700; letter-spacing: 0.04em; padding: 2px 7px;
                       border-radius: 4px; margin-bottom: 5px; }}
            .verdict-pass {{ background-color: #d4edda; color: #1b5e2a; }}
            .verdict-reject {{ background-color: #f8d7da; color: #86212b; }}
            .verdict-pending {{ background-color: #ededed; color: #5c5c5c; }}
            .claim-text {{ color: var(--ink-soft); font-size: 12.5px; line-height: 1.45; }}
            .claim-text.is-clamped {{ display: -webkit-box; -webkit-line-clamp: 3;
                                     -webkit-box-orient: vertical; overflow: hidden; }}
            /* Show more, without JavaScript: a visually hidden checkbox drives the
               clamp and the label text via sibling selectors. */
            .claim-toggle {{ position: absolute; width: 0; height: 0; opacity: 0; }}
            .claim-toggle:checked ~ .claim-text.is-clamped {{ display: block; -webkit-line-clamp: none; }}
            .claim-more {{ display: inline-block; margin-top: 5px; font-family: var(--sans);
                          font-size: 11px; font-weight: 600; color: #0b5cad; cursor: pointer; }}
            .claim-more:hover {{ color: #083f76; text-decoration: underline; }}
            .claim-more::after {{ content: "Show more"; }}
            .claim-toggle:checked ~ .claim-more::after {{ content: "Show less"; }}
            .claim-toggle:focus-visible ~ .claim-more {{ outline: 2px solid #0b5cad; outline-offset: 2px; }}

            td.links {{ white-space: nowrap; }}
            .link-row {{ display: flex; gap: 6px; }}
            .chip {{ font-family: var(--sans); font-size: 11px; font-weight: 600;
                    color: #333; text-decoration: none; background-color: #fff;
                    border: 1px solid #d0d0d0; border-radius: 5px; padding: 3px 9px; }}
            .chip:hover {{ color: #000; border-color: var(--ink); background-color: #fafafa; }}

            .pill {{ display: inline-block; font-family: var(--mono); font-size: 11.5px;
                    font-weight: 700; letter-spacing: 0.04em; padding: 3px 9px; border-radius: 4px; }}
            .state-PENDING {{ background-color: #ededed; color: #5c5c5c; }}
            .state-RUNNING {{ background-color: #cfe2ff; color: #0b4a8f; }}
            .state-VERIFYING {{ background-color: #e4d9f7; color: #4b2d80; }}
            .state-READY {{ background-color: #d4edda; color: #1b5e2a; }}
            .state-MERGED {{ background-color: #eef2ee; color: #5f7566; }}
            .state-QUARANTINED {{ background-color: #f8d7da; color: #86212b; }}
            .state-FAILED {{ background-color: #f8d7da; color: #86212b; }}
            .state-BLOCKED {{ background-color: #ffe5b4; color: #8a5200; }}
            /* Modifier on the state, not a state of its own — hence the flat treatment. */
            td.state {{ white-space: nowrap; }}
            .flag {{ display: inline-block; margin-left: 6px; font-family: var(--mono);
                    font-size: 11px; font-weight: 700; letter-spacing: 0.04em; color: #8a5200; }}
            .flag::before {{ content: "⚠ "; }}
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1>COGNITION-PROJECT</h1>
                <div class="subhead">Today {acus_today:.1f} / {config.DAILY_ACU_CEILING} ACU · last tick {last_tick_str}</div>
            </div>
            <form action="/scan" method="post">
                <button type="submit">Scan Now</button>
            </form>
        </div>

        {halt_banner}
        {exception_banner}

        <div class="stats">
            {_stat(total_findings, 'Findings', not total_findings)}
            {_stat(needs_attention, 'Need attention', not needs_attention)}
            {_stat(verified, 'Ready to merge', not verified)}
            {_stat(merged, 'Merged', not merged)}
        </div>
        <div class="stats-detail">
            {_count(needs_human, 'blocked')} · {_count(quarantined, 'quarantined')} · {_count(failed, 'failed')}
        </div>

        <div class="metrics">
            <div>
                <div class="metric-label">Trust</div>
                <div class="metric-value">{trust_rate_str}</div>
                <div class="metric-detail">{agent_claims} claims · {verifier_confirmed} confirmed · {verifier_caught} caught</div>
            </div>
            <div>
                <div class="metric-label">Cost</div>
                <div class="metric-value">{total_acus_all:.1f} ACU</div>
                <div class="metric-detail">{merged_prs} merged PRs{f' · avg find→verified {avg_cycle_time:.0f}m' if avg_cycle_time else ''}</div>
            </div>
            <div>
                <div class="metric-label">Gates</div>
                <div class="metric-value">{sum(gate_failures.values()) if gate_failures else 'none'}</div>
                <div class="metric-detail">{gates_str}</div>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th>Fingerprint</th>
                    <th>State</th>
                    <th class="num">Attempts</th>
                    <th class="num">ACU</th>
                    <th class="num">Age</th>
                    <th>Verdict &amp; Claim</th>
                    <th>Links</th>
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
        
        # Calculate age
        created_at = task.get("created_at")
        age_str = "—"
        if created_at:
            try:
                created = datetime.fromisoformat(created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                age_seconds = (now - created).total_seconds()
                if age_seconds < 3600:
                    age_str = f"{int(age_seconds / 60)}m"
                elif age_seconds < 86400:
                    age_str = f"{int(age_seconds / 3600)}h"
                else:
                    age_str = f"{int(age_seconds / 86400)}d"
            except (ValueError, TypeError):
                age_str = "—"
        
        # Build the verdict & claim column
        claim_verdict = build_claim_verdict(task)

        # "Stalled" — marked RUNNING but no session ever came back. It is a modifier
        # on the state, not an independent fact, so it rides along in the state cell
        # rather than owning a column that is empty on every other row.
        # Note: orphaned sessions are detected at the reconciler level, not per-task
        stalled = state == "RUNNING" and not task.get("session_id")
        state_cell = f'<span class="pill state-{escape(str(state))}">{escape(str(state))}</span>'
        if stalled:
            state_cell += '<span class="flag">stalled</span>'

        # Build links
        issue_number = task.get("issue_number")
        session_url = task.get("session_url")
        
        links = []
        if issue_number:
            links.append(f'<a class="chip" href="https://github.com/{config.GITHUB_REPO}/issues/{escape(str(issue_number))}">Issue</a>')
        if session_url:
            links.append(f'<a class="chip" href="{escape(str(session_url))}">Session</a>')
        
        # Chips on a single nowrap row. Underlined text links read as prose, go ragged
        # when they wrap, and leave a dangling separator behind them.
        links_str = f'<div class="link-row">{"".join(links)}</div>' if links else _muted("—")

        html += f"""
                <tr>
                    <td>{escape(str(fp))}</td>
                    <td class="state">{state_cell}</td>
                    <td class="num">{_muted(attempt_count, not attempt_count)}</td>
                    <td class="num">{_muted(f'{acus:.1f}', not acus)}</td>
                    <td class="num">{_muted(age_str, age_str == "—")}</td>
                    <td class="claim-verdict">{claim_verdict}</td>
                    <td class="links">{links_str}</td>
                </tr>
        """

    html += """
            </tbody>
        </table>
    </body>
    </html>
    """
    
    return html


def _gate_lists(evidence_json) -> tuple:
    """Pull (passed, skipped) gate names out of an attempt's stored evidence."""
    if not evidence_json:
        return [], []
    try:
        evidence = json.loads(evidence_json) if isinstance(evidence_json, str) else evidence_json
    except (json.JSONDecodeError, TypeError):
        return [], []
    if not isinstance(evidence, dict):
        return [], []
    return evidence.get("gates_passed") or [], evidence.get("gates_skipped") or []


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
            SELECT attempt_no, claim_json, verdict_passed, gate_failed, evidence_json
            FROM attempts
            WHERE fp = ?
            ORDER BY attempt_no
        """, (fp,))
        attempts = [dict(row) for row in cursor.fetchall()]
    
    if not attempts:
        return _muted("—")

    parts = []
    for attempt in attempts:
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
            # "PASS" on its own invites the reader to assume every gate ran.
            # Name the gates, and name any that were skipped.
            passed_gates, skipped_gates = _gate_lists(attempt.get("evidence_json"))
            detail = ", ".join(passed_gates)
            if skipped_gates:
                detail += f"; {', '.join(skipped_gates)} skipped"
            verdict = f"PASS ({detail})" if detail else "PASS"
            verdict_class = "verdict-pass"
        elif verdict_passed == 0:
            verdict = f"REJECT ({gate_failed})" if gate_failed else "REJECT"
            verdict_class = "verdict-reject"
        else:  # verdict_passed is None
            verdict, verdict_class = "PENDING", "verdict-pending"

        # The verdict leads and is never clipped — it is what a human scans this
        # column for. The claim is the agent's own self-report, so it is escaped.
        safe_claim = escape(str(claim_text))

        # Long claims clamp to 3 lines behind a Show more toggle. The toggle is a
        # hidden checkbox rather than JavaScript, so the page stays script-free.
        # Short claims fit already, so they skip the control entirely — roughly
        # three lines at this column width and type size.
        if len(str(claim_text)) > CLAIM_CLAMP_CHARS:
            toggle_id = f"claim-{escape(str(fp))}-{attempt.get('attempt_no', 0)}"
            body = (
                f'<input type="checkbox" class="claim-toggle" id="{toggle_id}">'
                f'<div class="claim-text is-clamped">{safe_claim}</div>'
                f'<label class="claim-more" for="{toggle_id}"></label>'
            )
        else:
            body = f'<div class="claim-text">{safe_claim}</div>'

        parts.append(
            f'<div class="attempt">'
            f'<span class="verdict {verdict_class}">{escape(verdict)}</span>'
            f"{body}"
            f"</div>"
        )

    return "".join(parts)


@app.post("/scan")
async def trigger_scan() -> Response:
    """Trigger a scan for demo purposes.
    
    This allows the demo to work without waiting for a webhook to arrive on cue.
    """
    try:
        # Scan the repo under review, not the directory this process runs in.
        findings = scanner.scan(config.TARGET_REPO_PATH)

        # Sync findings to database
        scanner.sync_findings(findings)
        
        return Response(content=f"Scan complete. {len(findings)} findings synced.", status_code=200)
    except Exception as e:
        return Response(content=f"Scan failed: {str(e)}", status_code=500)


@app.get("/metrics.json")
async def metrics_json() -> JSONResponse:
    """Return metrics as JSON for programmatic access.
    
    Same data as the status page metrics, but parseable.
    """
    db_path = config.DB_PATH
    
    # Check if database exists
    if not os.path.exists(db_path):
        return JSONResponse(content={"error": "No database found"}, status_code=404)
    
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        
        # Count by state
        cursor = conn.execute("""
            SELECT state, COUNT(*) as count
            FROM tasks
            GROUP BY state
        """)
        state_counts = {row["state"]: row["count"] for row in cursor.fetchall()}
        
        # TRUST: agent claims vs verifier confirmations
        cursor = conn.execute("""
            SELECT 
                COUNT(CASE WHEN verdict_passed = 1 THEN 1 END) as confirmed,
                COUNT(CASE WHEN verdict_passed = 0 THEN 1 END) as caught,
                COUNT(*) as total_claims
            FROM attempts
            WHERE verdict_passed IS NOT NULL
        """)
        trust_result = cursor.fetchone()
        agent_claims = trust_result["total_claims"] if trust_result else 0
        verifier_confirmed = trust_result["confirmed"] if trust_result else 0
        verifier_caught = trust_result["caught"] if trust_result else 0
        
        # COST: ROI calculation
        cursor = conn.execute("""
            SELECT 
                SUM(a.acus_consumed) as total_acus
            FROM attempts a
            WHERE a.ended_at IS NOT NULL
        """)
        cost_result = cursor.fetchone()
        total_acus_all = cost_result["total_acus"] if cost_result and cost_result["total_acus"] else 0.0
        
        # Count merged PRs separately
        cursor = conn.execute("""
            SELECT COUNT(*) as merged_prs
            FROM tasks
            WHERE state = 'MERGED'
        """)
        merged_result = cursor.fetchone()
        merged_prs = merged_result["merged_prs"] if merged_result and merged_result["merged_prs"] else 0
        
        # GATES: rejections by gate
        cursor = conn.execute("""
            SELECT gate_failed, COUNT(*) as count
            FROM attempts
            WHERE gate_failed IS NOT NULL AND gate_failed != ''
            GROUP BY gate_failed
        """)
        gate_results = cursor.fetchall()
        gate_failures = {row["gate_failed"]: row["count"] for row in gate_results}
        
        # Avg cycle time
        cursor = conn.execute("""
            SELECT 
                AVG((julianday(a.ended_at) - julianday(t.created_at)) * 24 * 60) as avg_minutes
            FROM attempts a
            JOIN tasks t ON a.fp = t.fp
            WHERE a.verdict_passed = 1 AND a.ended_at IS NOT NULL
        """)
        cycle_time_result = cursor.fetchone()
        avg_cycle_time = cycle_time_result["avg_minutes"] if cycle_time_result and cycle_time_result["avg_minutes"] else None
        
        # Today's ACU usage
        cursor = conn.execute("""
            SELECT SUM(acus_consumed) as total
            FROM attempts
            WHERE date(ended_at) = date('now')
        """)
        acu_result = cursor.fetchone()
        acus_today = acu_result["total"] if acu_result and acu_result["total"] else 0.0
        
        # Build state counts
        pending = state_counts.get("PENDING", 0)
        running = state_counts.get("RUNNING", 0) + state_counts.get("VERIFYING", 0)
        verified = state_counts.get("READY", 0)
        merged = state_counts.get("MERGED", 0)
        quarantined = state_counts.get("QUARANTINED", 0)
        needs_human = state_counts.get("BLOCKED", 0)
        failed = state_counts.get("FAILED", 0)
        total_findings = pending + running + verified + merged + quarantined + needs_human + failed
        
        # Liveness info
        last_tick_time = None
        last_tick_exception = None
        orphaned_count = 0
        try:
            import reconciler
            last_tick_time = reconciler.get_last_tick_time()
            last_tick_exception = reconciler.get_last_tick_exception()
            orphaned_count = reconciler.get_orphaned_session_count()
        except ImportError:
            pass
        
        # Format last tick time
        last_tick_str = None
        if last_tick_time:
            now = datetime.now(timezone.utc)
            # Ensure both datetimes are timezone-aware
            if last_tick_time.tzinfo is None:
                last_tick_time = last_tick_time.replace(tzinfo=timezone.utc)
            seconds_ago = (now - last_tick_time).total_seconds()
            if seconds_ago < 60:
                last_tick_str = f"{int(seconds_ago)}s ago"
            else:
                minutes_ago = int(seconds_ago / 60)
                last_tick_str = f"{minutes_ago}m ago"

        return JSONResponse(content={
            "trust": {
                "agent_claims": agent_claims,
                "verifier_confirmed": verifier_confirmed,
                "verifier_caught": verifier_caught
            },
            "flow": {
                "total_findings": total_findings,
                "pending": pending,
                "running": running,
                "verified": verified,
                "merged": merged,
                "quarantined": quarantined,
                "needs_human": needs_human,
                "failed": failed
            },
            "cost": {
                "total_acus": total_acus_all,
                "merged_prs": merged_prs,
                "avg_cycle_time_minutes": avg_cycle_time
            },
            "gates": gate_failures,
            "liveness": {
                "last_tick": last_tick_str,
                "last_tick_exception": last_tick_exception,
                "orphaned_sessions": orphaned_count
            },
            "budget": {
                "acus_today": acus_today,
                "daily_acu_ceiling": config.DAILY_ACU_CEILING
            }
        })


def should_tick_immediately() -> bool:
    """Check if a webhook was received and reset the flag."""
    global _webhook_received
    if _webhook_received:
        _webhook_received = False
        return True
    return False