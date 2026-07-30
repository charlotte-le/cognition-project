# cognition-project

## Quick Start

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env with your Devin API credentials and GitHub token

# 2. Start the service (Docker - recommended)
docker compose up

# 3. Access the status page
open http://localhost:8000/status

# 4. Trigger a scan
curl -X POST http://localhost:8000/scan
```

## What This Is

cognition-project runs a closed loop against a target repository:

**scan → file an issue → hand it to Devin → independently verify the resulting PR → either put it in front of a human, or send the evidence back to the same session for one repair attempt.**

## Project Structure

```
cognition-project/
├── cognition/                    # Main package
│   ├── core/                     # Core orchestration & data layer
│   │   ├── db.py                # SQLite database operations
│   │   ├── config.py            # Configuration management
│   │   ├── reconciler.py        # Main orchestration loop
│   │   └── prompts.py           # AI prompt templates
│   ├── verification/             # Verification & scanning
│   │   ├── scanner.py           # Bandit scanner integration
│   │   └── verifier.py          # Five-gate verification engine
│   ├── api/                      # External API clients
│   │   ├── devin.py             # Devin API client
│   │   └── github.py            # GitHub API client
│   └── web/                      # Web interface
│       └── web.py               # FastAPI application
├── tests/                        # Test suite
│   ├── test_db.py
│   ├── test_reconciler.py
│   ├── test_verifier.py
│   ├── test_metrics.py
│   ├── test_scanner.py
│   ├── test_triggers.py
│   └── smoke.py
├── main.py                       # Application entry point
├── Dockerfile                    # Container definition
├── docker-compose.yml            # Container orchestration
├── .env.example                  # Environment variables template
└── requirements.txt              # Python dependencies
```

## Architecture

```
  scheduled scan  ─┐
  (or "scan now")  │   findings      ┌──────────────────────────────┐
                   ├────────────────►│         RECONCILER           │
  webhook: issue  ─┘                 │  every 30s — or immediately, │
  labeled auto                       │  when a webhook wakes it     │
                                     │                              │
                                     │  desired : open labeled      │
                                     │            issues            │
                                     │  observed: ledger ⋈ live     │
                                     │            sessions ⋈ PRs    │
                                     │  → one transition per task   │
                                     └────┬────────────────────┬────┘
                                          │                    │
                                 create / message          harvest PR
                                          ▼                    ▼
                                  ┌───────────────┐   ┌─────────────────┐
                                  │     DEVIN     │   │    VERIFIER     │ subprocess
                                  │   playbook    │   │  policy gate    │ hard timeout
                                  │   knowledge   │   │   + oracle      │
                                  │   ACU cap     │   └───┬─────────┬───┘
                                  │   schema      │   pass│         │fail
                                  └───────────────┘       ▼         ▼
                                                   ready-for-   message the
                                                   review +     same session
                                                   evidence     once, then
                                                                quarantine
```

## State Machine

```
PENDING ──► RUNNING ──► VERIFYING ──► READY ──► MERGED
                │            │               │
                │            │               │
                ▼            ▼               │
            BLOCKED     QUARANTINED ◄───────┘
                │            ▲
                │            │ (retry failed)
                └────────────┘
                (resume)
```

**Additional transitions:**
- RUNNING → FAILED (session failed, timeout, or HALT signal)
- BLOCKED → VERIFYING (if PR created while blocked)
- READY → CLOSED (human reviewer closed the PR without merging)

## Running the Workflow

The service runs as a single container handling both the reconciler loop and web interface.

### Option 1: Docker (Recommended)

**Prerequisites:**
- Docker and Docker Compose installed
- Devin API credentials and GitHub token
- Target repository checkout (e.g., `apache/superset` fork)

**Setup:**

```bash
# 1. Configure environment (see Environment Variables below)
cp .env.example .env

# 2. Point docker-compose.yml's TARGET_REPO_HOST_PATH at your checkout
#    (default: ../superset, relative to the project directory)

# 3. Start the service
docker compose up
# Older Docker Compose v1: use `docker-compose up` instead
```

### Option 2: Local Development

**Prerequisites:**
- Python 3.11+
- Bandit 1.7.9 (`pip install bandit==1.7.9`)
- Target repository checkout

**Setup:**

```bash
pip install -r requirements.txt
cp .env.example .env   # edit credentials + TARGET_REPO_PATH
python main.py
```

Either way, once it's running:
- Status page: http://localhost:8000/status
- Scan trigger: `curl -X POST http://localhost:8000/scan`
- Webhook endpoint: `http://localhost:8000/webhook` (Docker only, for GitHub integration)

### What Triggers Work

Three things start work, in descending order of importance:

| Trigger | What it does | Cadence |
|---------|--------------|---------|
| **Scheduled scan** | Re-runs Bandit, files issues for new findings | `SCAN_INTERVAL_MINUTES` (default 60, plus once at startup) |
| **GitHub webhook** | Wakes the reconciler immediately | On delivery |
| **Reconciler tick** | Reconciles desired vs observed state | `TICK_SECONDS` (default 30) |

The scan puts work into the system; webhook and tick move it through. The webhook is an optimization, not a dependency—the loop is idempotent. Scanning is also idempotent: findings are keyed by fingerprint, so repeat scans don't re-file known issues.

### Workflow Steps

1. **Scan** — Bandit runs against the target repo (at startup, then every `SCAN_INTERVAL_MINUTES`, or on demand via "Scan Now"). Findings become GitHub issues labeled `cognition-project:auto`.
2. **Process** — The reconciler picks up labeled issues and hands each to a Devin session, bounded by ACU and time limits.
3. **Verify** — When a session opens a PR, the verifier runs (see [Five Gates](#the-verifier)). Pass → READY; fail → one repair attempt, then QUARANTINED.
4. **Review** — A human merges (or closes) READY PRs. Merging closes the issue and marks the task MERGED.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DEVIN_API_BASE` | Devin API base URL | `https://api.devin.ai` |
| `DEVIN_API_KEY` | Devin API key | `demo-key` |
| `DEVIN_ORG_ID` | Devin organization ID | `demo-org` |
| `GITHUB_TOKEN` | GitHub personal access token | `demo-token` |
| `GITHUB_REPO` | Target repository | `owner/repo` |
| `TARGET_REPO_PATH` | Path to target repo checkout | `/repo` |
| `DRY_RUN` | Enable demo mode (no real API calls) | `true` |
| `TICK_SECONDS` | Reconciler loop interval | `30` |
| `SCAN_INTERVAL_MINUTES` | Scheduled scan interval; `0` disables schedule | `60` |
| `MAX_CONCURRENT` | Max concurrent sessions | `3` |
| `MAX_ACU_PER_SESSION` | Per-session ACU cap | `12` |
| `HUMAN_FIX_MINUTES` | Estimated time for human to fix one finding | `45` |
| `RUN_TESTS_GATE` | Enable test verification gate | `false` |

### GitHub Webhook Integration (Optional)

For faster response to issue events, configure a GitHub webhook:

1. Go to your repository Settings → Webhooks
2. Add webhook: `http://your-server:8000/webhook`
3. Select events: Issues (or use "Send me everything")
4. A delivery wakes the reconciler immediately rather than waiting out the remainder of `TICK_SECONDS`

## The Verifier

The verifier runs as a subprocess with a hard timeout and answers one question: did the change the agent claims it made actually do what it says?

### Five Gates

| # | Gate | Checks |
|---|------|--------|
| 1 | **Join** | Footer fingerprint matches, `Fixes #<n>` resolves to the right issue, head SHA is current |
| 2 | **Policy** | Files ⊆ allowlist; diff ≤ 60 LOC; no `# nosec` / `# noqa` / `# type: ignore` added; no dependency-file changes |
| 3 | **Oracle** | Re-run the byte-identical pinned Bandit command: fingerprint absent **and** no new findings |
| 4 | **Tests** | The mapped test subset is green |
| 5 | **Publish** | Labels, evidence comment on the issue |

**Merge is human, always.** When a human merges, the reconciler marks the task `MERGED` and closes the issue.

## Scale Metrics

The system tracks metrics that answer the questions engineering leaders actually ask about autonomous remediation:

### Outcome Metrics

- **Autonomy rate** — Percentage of tasks that reached the review gate (READY, MERGED, or CLOSED) without requiring human intervention mid-flight. Excludes the final review decision, which is always human.

- **First-pass yield** — Percentage of tasks that passed verification on the first attempt, out of all tasks that received a verdict. Measures the quality of the agent's initial work.

- **Hours returned** — Estimated human hours saved: `(verified tasks) × HUMAN_FIX_MINUTES / 60`. Counts only READY and MERGED tasks (verified work the team didn't have to do).

### Trust Metrics

- **Agent claims vs. verifier confirmation** — Verifier's decision on each agent claim: confirmed (the fix actually did what the agent claimed) or caught (the claim was false). Tracks reliability of agent self-reporting.

### Throughput Metrics

- **Merged PRs** — Total count of tasks that reached MERGED state (human reviewed and approved).
- **Average discovery→review time** — Mean minutes from a finding's first Devin session to its `ready-for-review` label. Starts at first session (not scan time, so queue backlog isn't counted) and stops at verification passing (so reviewer wait time isn't counted). Includes repair attempts, so the retry loop isn't free. Verified tasks only.

### Operational Metrics

- **Gate failure breakdown** — Which verification gates are catching failures (policy, oracle, tests, etc.).
- **State distribution** — Current counts by task state (pending, running, verified, merged, quarantined, needs human, failed, closed).

## Development

### Running Tests

Run the test suite:

```bash
pytest tests/
```

Or run specific test files:

```bash
pytest tests/test_db.py
pytest tests/test_reconciler.py
pytest tests/test_verifier.py
pytest tests/test_metrics.py
pytest tests/test_scanner.py
pytest tests/test_triggers.py
```

Run the smoke test for basic functionality:

```bash
pytest tests/smoke.py
```

## Troubleshooting

### Common Issues

**Database not found error**
- Run a scan first to initialize the database: `curl -X POST http://localhost:8000/scan`

**Devin API connection failed**
- Verify `DEVIN_API_KEY` and `DEVIN_ORG_ID` are set correctly in `.env`
- Check that your API key has the `ManageOrgSessions` permission

**GitHub webhook not triggering**
- Ensure the webhook URL is accessible from GitHub (use ngrok or similar for local development)
- Verify the webhook is configured for "Issues" events
- Confirm it is arriving: a delivery logs `Webhook received - ticking immediately`

**Scheduled scan not running**
- Check `last scan` in the status page header — `never` with the schedule on means the first scan has not finished or has failed
- A failed scan shows its reason in a red banner and in `/metrics.json` under `liveness.last_scan_error`
- `SCAN_INTERVAL_MINUTES=0` disables the schedule; the header says `(schedule off)`

**Bandit scan returns no findings**
- Check that `TARGET_REPO_PATH` points to a valid repository checkout
- Verify `RULE_ALLOWLIST` includes the Bandit rule IDs you want to detect
- Ensure the target repository contains Python code to scan

**Verification gate fails with "infra" error**
- This indicates an environment issue, not an agent failure
- Check that Bandit is installed and accessible: `bandit --version`
- Verify the target repository path is correct and accessible
- If using the tests gate, ensure test dependencies are installed

**Task stuck in BLOCKED state**
- The agent asked a question and is waiting for human input
- Visit the Devin session URL (shown in the status page) to respond
- Or answer directly on the GitHub issue

**HALT latched error**
- The system received a HALT signal from the Devin platform
- No new sessions will be created until the latch is cleared
- Check the Devin platform for platform-wide issues or limits

**PR closed without merging**
- If a human reviewer closes a PR without merging, the task moves to CLOSED state
- This is distinct from QUARANTINED (harness rejected) and FAILED (machinery broke)
- CLOSED tasks represent a review decision that the fix should not be merged
- These tasks are not retried automatically
