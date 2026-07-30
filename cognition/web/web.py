"""Web interface for cognition-project.

FastAPI with four routes:
- POST /webhook — dumb webhook that triggers immediate reconciler tick
- GET /status — server-rendered HTML status page, no JavaScript
- POST /scan — manual scan trigger for demo purposes
- GET /metrics.json — programmatic access to metrics (parseable JSON)
"""

import asyncio
import sqlite3
import os
import json
from datetime import datetime, timezone
from html import escape
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from cognition.core import config, db, reconciler
from cognition.verification import scanner

app = FastAPI(title="cognition-project")


# Set by POST /webhook, awaited by the reconciler loop.
#
# An Event rather than a bool because the loop has to be able to *block* on it.
# The bool version was read once at the top of each iteration, after the full
# TICK_SECONDS sleep had already elapsed, so a delivery never bought back any
# latency - it only logged a line. The loop now sleeps on this Event with the
# tick interval as a timeout, so a delivery wakes it immediately.
#
# Built on first use inside the running loop rather than at import: before
# Python 3.10 an Event binds to whichever loop exists when it is constructed,
# and at import time that is either the wrong loop or no loop at all.
_webhook_event: Optional[asyncio.Event] = None
_webhook_loop: Optional[asyncio.AbstractEventLoop] = None


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


def _ago(when: Optional[datetime], never: str = "never") -> str:
    """Render a timestamp as "15s ago" / "4m ago", or `never` if there is none.

    The liveness globals are written with utcnow() and so are naive; anything
    naive is read as UTC rather than as local time, which is what kept the
    "last tick" reading sane across a container in one zone and a browser in
    another.
    """
    if when is None:
        return never

    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    seconds_ago = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds_ago < 60:
        return f"{int(seconds_ago)}s ago"
    return f"{int(seconds_ago / 60)}m ago"


def _stat(value: str, label: str, is_zero: bool = False) -> str:
    """One tile in the headline stat strip: big value over a small caps label."""
    cls = "stat is-zero" if is_zero else "stat"
    return (
        f'<div class="{cls}">'
        f'<div class="stat-value">{escape(str(value))}</div>'
        f'<div class="stat-label">{escape(label)}</div>'
        f"</div>"
    )


# A task is counted for autonomy once it has an outcome to judge, or has stopped
# to wait for a person. PENDING/RUNNING/VERIFYING are still in flight and say
# nothing yet, so they stay out of the denominator rather than dragging the rate
# down for the crime of being recent.
_RESOLVED_STATES = ("READY", "MERGED", "CLOSED", "QUARANTINED", "FAILED", "BLOCKED")

# Of those, the ones the machine delivered all the way to the review gate. CLOSED
# belongs here: the reviewer declined the change, but the system still got it in
# front of them unattended, which is what this metric asks. Whether reviewers say
# yes is a different question, and conflating the two would let a good pipeline
# with a picky reviewer read as a system that cannot work on its own.
_DELIVERED_STATES = ("READY", "MERGED", "CLOSED")


def _avg_discovery_to_review_minutes(conn: sqlite3.Connection) -> Optional[float]:
    """Mean minutes from discovery to a PR a human can review.

    Discovery is the moment the first session on a finding started, not the
    moment the scanner wrote the row. The gap between those two is queue depth -
    how long the backlog was when the finding landed - and folding it in makes
    the pipeline look slow on a busy day and fast on a quiet one without
    anything about the pipeline having changed. Anchoring on the session start
    measures the part this system actually controls.

    The clock stops when the verifier passes the change, because that is when
    the PR gets its ready-for-review label and a person can pick it up. Time it
    then spends waiting on a reviewer is the reviewer's queue, not throughput.

    MIN(started_at) rather than the passing attempt's own start: when a task
    needed three sessions to land, the wait for review really was all three.
    Timing only the last one would make the repair loop look free, and reward
    a change that fails twice over one that lands first time.

    Shared by the status page and /metrics.json so the two cannot disagree.
    """
    row = conn.execute(
        """
        SELECT AVG((julianday(passed.ended_at) - julianday(first.started_at))
                   * 24 * 60) AS avg_minutes
        FROM attempts passed
        JOIN (
            SELECT fp, MIN(started_at) AS started_at FROM attempts GROUP BY fp
        ) first ON first.fp = passed.fp
        WHERE passed.verdict_passed = 1 AND passed.ended_at IS NOT NULL
        """
    ).fetchone()
    return row["avg_minutes"] if row and row["avg_minutes"] else None


def _outcome_metrics(conn: sqlite3.Connection) -> dict:
    """Compute the three metrics an engineering leader actually asks for.

    Autonomy rate  - did the machine get there without us?
    First-pass yield - was it right the first time, or did it need the repair loop?
    Hours returned - what was that worth?

    Shared by the status page and /metrics.json so the two cannot disagree about
    the same question.
    """
    resolved_ph = ",".join("?" * len(_RESOLVED_STATES))
    delivered_ph = ",".join("?" * len(_DELIVERED_STATES))

    # AUTONOMY: reached the review gate with no human pulled in along the way.
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS resolved,
            COUNT(CASE WHEN state IN ({delivered_ph}) AND human_touched = 0
                       THEN 1 END) AS autonomous,
            COUNT(CASE WHEN human_touched = 1 THEN 1 END) AS interrupted
        FROM tasks
        WHERE state IN ({resolved_ph})
        """,
        (*_DELIVERED_STATES, *_RESOLVED_STATES),
    ).fetchone()
    resolved = row["resolved"] or 0
    autonomous = row["autonomous"] or 0
    interrupted = row["interrupted"] or 0

    # FIRST-PASS YIELD: passed on attempt 1, out of every task the verifier ever
    # returned a verdict on. Counted over tasks rather than attempts - a task
    # that failed twice is one task that did not land first time, not two.
    row = conn.execute(
        """
        SELECT
            COUNT(DISTINCT fp) AS judged,
            COUNT(DISTINCT CASE WHEN attempt_no = 1 AND verdict_passed = 1
                                THEN fp END) AS first_pass
        FROM attempts
        WHERE verdict_passed IS NOT NULL
        """
    ).fetchone()
    judged = row["judged"] or 0
    first_pass = row["first_pass"] or 0

    # HOURS RETURNED: verified work the team did not have to do. READY counts -
    # the fix exists and is waiting on a merge button, and the engineering time
    # it replaces was already spent by the agent. The assumption behind the
    # multiplier is printed next to the number.
    row = conn.execute(
        f"SELECT COUNT(*) AS remediated FROM tasks WHERE state IN ({delivered_ph})",
        _DELIVERED_STATES,
    ).fetchone()
    # CLOSED is delivered but not remediated - the finding is still open, so it
    # buys back nothing. Subtract it back out rather than widening the tuple.
    closed = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE state = 'CLOSED'"
    ).fetchone()["n"] or 0
    remediated = (row["remediated"] or 0) - closed
    hours_returned = remediated * config.HUMAN_FIX_MINUTES / 60

    return {
        "autonomy": {
            "rate": (autonomous / resolved) if resolved else None,
            "autonomous": autonomous,
            "resolved": resolved,
            "interrupted": interrupted,
        },
        "first_pass": {
            "rate": (first_pass / judged) if judged else None,
            "first_pass": first_pass,
            "judged": judged,
        },
        "hours_returned": {
            "hours": hours_returned,
            "remediated": remediated,
            "assumed_minutes_per_fix": config.HUMAN_FIX_MINUTES,
        },
    }


def _pct(rate: Optional[float]) -> str:
    """Render a rate as a percentage, or an em-dash when there is nothing to rate."""
    return f"{rate * 100:.0f}%" if rate is not None else "—"


def _hours(hours: float) -> str:
    """Hours at one decimal until they get big enough that the decimal is noise."""
    if not hours:
        return "0h"
    return f"{hours:.1f}h" if hours < 10 else f"{hours:.0f}h"


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
    webhook_signal().set()
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
            SELECT t.*, a.session_id, a.session_url, a.pr_url, a.verdict_passed, a.gate_failed,
                   a.claim_json, a.evidence_json
            FROM tasks t
            LEFT JOIN attempts a ON t.current_attempt_id = a.id
            ORDER BY t.updated_at DESC
        """)
        tasks = [dict(row) for row in cursor.fetchall()]

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

        # THROUGHPUT: work that made it all the way to merged
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
        
        # Average time from discovery to a PR waiting on a human.
        avg_cycle_time = _avg_discovery_to_review_minutes(conn)

        # OUTCOMES: autonomy, first-pass yield, hours returned.
        outcomes = _outcome_metrics(conn)

    autonomy = outcomes["autonomy"]
    first_pass = outcomes["first_pass"]
    returned = outcomes["hours_returned"]

    # Build counts
    total_findings = sum(state_counts.values())

    pending = state_counts.get("PENDING", 0)
    running = state_counts.get("RUNNING", 0) + state_counts.get("VERIFYING", 0)
    verified = state_counts.get("READY", 0)
    merged = state_counts.get("MERGED", 0)
    quarantined = state_counts.get("QUARANTINED", 0)
    needs_human = state_counts.get("BLOCKED", 0)
    failed = state_counts.get("FAILED", 0)
    closed = state_counts.get("CLOSED", 0)
    needs_attention = needs_human + quarantined + failed

    # Sort tasks by what needs a human first, not just recency.
    # BLOCKED/QUARANTINED/FAILED need action now; READY is waiting on a merge;
    # RUNNING/VERIFYING/PENDING are in flight; MERGED and CLOSED are done.
    # CLOSED sorts last with MERGED: a human already ruled on it.
    state_priority = {
        "BLOCKED": 0, "QUARANTINED": 1, "FAILED": 2, "READY": 3,
        "RUNNING": 4, "VERIFYING": 4, "PENDING": 5, "MERGED": 6, "CLOSED": 6,
    }
    tasks.sort(key=lambda t: state_priority.get(t["state"], 99))

    # Check HALT latch and liveness (read the reconciler module globals directly)
    halt_banner = ""
    exception_banner = ""
    if reconciler._halt_latched:
        halt_banner = '<div class="banner banner-stop"><strong>HALT LATCHED</strong> — no new sessions allowed</div>'

    last_tick_str = _ago(reconciler.get_last_tick_time())

    # The scan is the trigger that feeds this whole pipeline, so its liveness
    # belongs next to the reconciler's: a loop ticking happily against a scanner
    # that died six hours ago looks identical to a healthy system otherwise.
    last_scan_str = _ago(scanner.get_last_scan_time())
    if config.SCAN_INTERVAL_MINUTES <= 0:
        last_scan_str += " (schedule off)"

    scan_error = scanner.get_last_scan_error()
    if scan_error:
        exception_banner += (
            f'<div class="banner banner-stop"><strong>Last scan failed:</strong> '
            f"{escape(str(scan_error))}</div>"
        )

    # Get last tick exception
    last_exception = reconciler.get_last_tick_exception()
    if last_exception:
        exception_banner += f'<div class="banner banner-stop"><strong>Last tick error:</strong> {escape(str(last_exception))}</div>'

    # Get orphaned session count
    orphaned_count = reconciler.get_orphaned_session_count()
    if orphaned_count > 0:
        exception_banner += (
            f'<div class="banner banner-warn"><strong>{orphaned_count} orphaned '
            f"session(s)</strong> detected</div>"
        )

    # Render the gate summary — escaped, since gate names come from attempt rows.
    gates_str = " · ".join(
        f"{escape(str(gate))} {count}" for gate, count in gate_failures.items()
    ) if gate_failures else '<span class="zero">no rejections</span>'

    # Build HTML.
    #
    # Case carries meaning here, so it is applied consistently:
    #   ALL CAPS    — machine enum values (PENDING, RUNNING, HALT LATCHED)
    #   Title Case  — labels we wrote (column headers, buttons)
    #   lowercase   — prose and values (15s ago, avg discovery→review 348m)
    # The demoted small labels (TRUST/THROUGHPUT/GATES, column headers) are a separate
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

            .stats {{ display: flex; flex-wrap: wrap; gap: 40px; margin-bottom: 26px; }}
            .stat-value {{ font-family: var(--mono); font-size: 26px; font-weight: 700; line-height: 1.15; }}
            .stat-label {{ color: var(--ink-mute); margin-top: 3px; }}
            .stat.is-zero .stat-value {{ color: var(--ink-mute); font-weight: 400; }}

            /* Two rows of three: outcomes on top (did it work, what was it
               worth), mechanism underneath (how do I know). The row gap is
               wider than the column gap so the two registers read as bands
               rather than one undifferentiated six-up grid. */
            .metrics {{ display: grid; grid-template-columns: repeat(3, 1fr);
                       column-gap: 28px; row-gap: 22px;
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
            /* A human declined it - settled, not an alarm. */
            .state-CLOSED {{ background-color: #ede9e3; color: #6b5f52; }}
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
                <div class="subhead">last tick {last_tick_str} · last scan {last_scan_str}</div>
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

        <div class="metrics">
            <div>
                <div class="metric-label">Autonomy</div>
                <div class="metric-value">{_pct(autonomy['rate'])}</div>
                <div class="metric-detail">{autonomy['autonomous']} of {autonomy['resolved']} resolved · {_count(autonomy['interrupted'], 'needed a human')}</div>
            </div>
            <div>
                <div class="metric-label">First-pass yield</div>
                <div class="metric-value">{_pct(first_pass['rate'])}</div>
                <div class="metric-detail">{first_pass['first_pass']} of {first_pass['judged']} passed on attempt 1</div>
            </div>
            <div>
                <div class="metric-label">Hours returned</div>
                <div class="metric-value">{_hours(returned['hours'])}</div>
                <div class="metric-detail">{returned['remediated']} remediated × {returned['assumed_minutes_per_fix']}m assumed</div>
            </div>
            <div>
                <div class="metric-label">Trust</div>
                <div class="metric-value">{trust_rate_str}</div>
                <div class="metric-detail">{agent_claims} claims · {verifier_confirmed} confirmed · {verifier_caught} caught</div>
            </div>
            <div>
                <div class="metric-label">Throughput</div>
                <div class="metric-value">{merged_prs} merged</div>
                <div class="metric-detail">{f'{avg_cycle_time:.0f}m avg discovery→review' if avg_cycle_time else 'no completed cycles yet'}</div>
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
        pr_url = task.get("pr_url")
        
        links = []
        if issue_number:
            links.append(f'<a class="chip" href="https://github.com/{config.GITHUB_REPO}/issues/{escape(str(issue_number))}" target="_blank">Issue</a>')
        if session_url:
            links.append(f'<a class="chip" href="{escape(str(session_url))}" target="_blank">Session</a>')
        if pr_url:
            links.append(f'<a class="chip" href="{escape(str(pr_url))}" target="_blank">PR</a>')
        
        # Chips on a single nowrap row. Underlined text links read as prose, go ragged
        # when they wrap, and leave a dangling separator behind them.
        links_str = f'<div class="link-row">{"".join(links)}</div>' if links else _muted("—")

        html += f"""
                <tr>
                    <td>{escape(str(fp))}</td>
                    <td class="state">{state_cell}</td>
                    <td class="num">{_muted(attempt_count, not attempt_count)}</td>
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


def _split_reason(reason: str) -> tuple:
    """Split a terminal reason into (badge cause, body detail).

    Reasons are written "<cause>: <detail>". The cause is short enough to sit in
    a verdict badge next to "REJECT (oracle)"; the detail belongs in the body
    text. A reason with no colon is already short, so it becomes the badge and
    the body stays empty.
    """
    cause, _, detail = str(reason).partition(": ")
    return cause.strip(), detail.strip()


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

    # A task can reach a terminal state without any gate ever returning a
    # verdict - the wall clock expiring, the session dying, a human closing the
    # PR. Those used to render as an em-dash, which is why a FAILED task on this
    # page gave no hint why it failed.
    reason = task.get("failure_reason")

    if not attempts:
        return escape(str(reason)) if reason else _muted("—")

    reason_shown = False
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
                    claim_text = claim.get("summary") or claim.get("fix_description")
                else:
                    claim_text = str(claim)
            except (json.JSONDecodeError, TypeError):
                claim_text = None
        else:
            claim_text = None

        # An attempt that has filed no claim gets no claim text. This used to
        # fall back to the literal "fixed", which put a fix the agent had never
        # reported - and on a RUNNING task, had not yet even finished attempting -
        # into the one column that exists to hold the agent to its own words.
        # Distinguished here so a real claim is never overwritten below.
        has_claim = bool(claim_text)
        
        # Add verdict
        verdict_passed = attempt.get("verdict_passed")
        gate_failed = attempt.get("gate_failed")
        
        if verdict_passed == 1:
            verdict, verdict_class = "PASS", "verdict-pass"
        elif verdict_passed == 0:
            verdict = f"REJECT ({gate_failed})" if gate_failed else "REJECT"
            verdict_class = "verdict-reject"
        elif reason and task.get("state") in ("FAILED", "CLOSED"):
            # No gate ever returned a verdict, but the task ended anyway - the
            # wall clock expired, the session died, a human closed the PR.
            # "PENDING" would read as still in flight; say what happened.
            #
            # Reasons are written "<cause>: <detail>" so the badge stays as
            # short as "REJECT (oracle)" and the detail drops into the body
            # text below it. Putting the whole sentence in the badge stretched
            # it into a pink slab across the row.
            cause, detail_text = _split_reason(reason)
            verdict, verdict_class = cause, "verdict-reject"
            # Only fill the body with the detail when there is no real claim to
            # displace; an agent's own words outrank our explanation of them.
            if detail_text and not has_claim:
                claim_text = detail_text
            reason_shown = True
        else:  # verdict_passed is None
            verdict, verdict_class = "PENDING", "verdict-pending"

        # The verdict leads and is never clipped — it is what a human scans this
        # column for. The claim is the agent's own self-report, so it is escaped.
        safe_claim = escape(str(claim_text)) if claim_text else ""

        # Long claims clamp to 3 lines behind a Show more toggle. The toggle is a
        # hidden checkbox rather than JavaScript, so the page stays script-free.
        # Short claims fit already, so they skip the control entirely — roughly
        # three lines at this column width and type size.
        if not claim_text:
            # Nothing claimed yet: the verdict badge stands alone rather than
            # captioning itself with words the agent never said.
            body = ""
        elif len(str(claim_text)) > CLAIM_CLAMP_CHARS:
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

    # A terminal reason the verdict chain does not already carry - a human
    # closing a PR that passed every gate, say - is the most important thing in
    # this column and would otherwise be invisible behind a green PASS.
    if reason and not reason_shown and task.get("state") in ("FAILED", "CLOSED"):
        cause, detail_text = _split_reason(reason)
        body = (
            f'<div class="claim-text">{escape(detail_text)}</div>' if detail_text else ""
        )
        parts.append(
            f'<div class="attempt">'
            f'<span class="verdict verdict-reject">{escape(cause)}</span>'
            f"{body}"
            f"</div>"
        )

    return "".join(parts)


@app.post("/scan")
async def trigger_scan() -> Response:
    """Trigger a scan on demand.

    The same scan the schedule runs, on the same code path - this only exists so
    a demo does not have to wait out SCAN_INTERVAL_MINUTES. Run in a thread:
    Bandit over superset/ takes long enough to stall the reconciler and every
    other request if it ran on the event loop.
    """
    try:
        count = await asyncio.to_thread(scanner.run_scan)
        return Response(content=f"Scan complete. {count} findings synced.", status_code=200)
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
        
        # THROUGHPUT: work that made it all the way to merged
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
        
        # Average time from discovery to a PR waiting on a human.
        avg_cycle_time = _avg_discovery_to_review_minutes(conn)

        # OUTCOMES: autonomy, first-pass yield, hours returned.
        outcomes = _outcome_metrics(conn)

        # Build state counts
        pending = state_counts.get("PENDING", 0)
        running = state_counts.get("RUNNING", 0) + state_counts.get("VERIFYING", 0)
        verified = state_counts.get("READY", 0)
        merged = state_counts.get("MERGED", 0)
        quarantined = state_counts.get("QUARANTINED", 0)
        needs_human = state_counts.get("BLOCKED", 0)
        failed = state_counts.get("FAILED", 0)
        closed = state_counts.get("CLOSED", 0)
        # Sum the map rather than named states: a state added later is counted
        # automatically instead of silently vanishing from the total.
        total_findings = sum(state_counts.values())
        
        # Liveness info
        last_tick_time = reconciler.get_last_tick_time()
        last_tick_exception = reconciler.get_last_tick_exception()
        orphaned_count = reconciler.get_orphaned_session_count()
        last_tick_str = _ago(last_tick_time, never=None)

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
                "failed": failed,
                "closed": closed
            },
            "throughput": {
                "merged_prs": merged_prs,
                "avg_discovery_to_review_minutes": avg_cycle_time
            },
            "autonomy": outcomes["autonomy"],
            "first_pass_yield": outcomes["first_pass"],
            "hours_returned": outcomes["hours_returned"],
            "gates": gate_failures,
            "liveness": {
                "last_tick": last_tick_str,
                "last_tick_exception": last_tick_exception,
                "orphaned_sessions": orphaned_count,
                # The trigger's own health. A consumer polling this endpoint has
                # to be able to tell "nothing to do" from "nothing is looking".
                "last_scan": _ago(scanner.get_last_scan_time(), never=None),
                "last_scan_findings": scanner.get_last_scan_findings(),
                "last_scan_error": scanner.get_last_scan_error(),
                "scan_interval_minutes": config.SCAN_INTERVAL_MINUTES
            }
        })


def webhook_signal() -> asyncio.Event:
    """The Event a webhook delivery sets. Must be called from inside the loop.

    The reconciler loop owns clearing it, and clears it *before* each tick so a
    delivery that lands mid-tick is not swallowed - it leaves the Event set and
    the loop re-ticks immediately rather than waiting out the interval.

    Rebuilt if the running loop is not the one it was built on. In the service
    there is only ever one loop; this keeps the module safe to drive from a test
    that runs several, which on Python 3.9 would otherwise wait on an Event
    bound to a loop that is already closed.
    """
    global _webhook_event, _webhook_loop

    loop = asyncio.get_running_loop()
    if _webhook_event is None or _webhook_loop is not loop:
        _webhook_event = asyncio.Event()
        _webhook_loop = loop

    return _webhook_event