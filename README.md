# cognition-project

Devin does the work, the verifier decides whether it worked, a human decides whether it ships.

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

cognition-project is a service that runs one closed loop against a fork of `apache/superset`:

**scan → file an issue → hand it to Devin → independently verify the resulting PR → either put it in front of a human, or send the evidence back to the same session for one repair attempt.**

## Project Structure

The codebase is organized as a proper Python package with clear separation of concerns:

```
cognition-project/
├── cognition/                    # Main package
│   ├── __init__.py
│   ├── core/                     # Core orchestration & data layer
│   │   ├── __init__.py
│   │   ├── db.py                # SQLite database operations
│   │   ├── config.py            # Configuration management
│   │   ├── reconciler.py        # Main orchestration loop
│   │   └── prompts.py           # AI prompt templates
│   ├── verification/             # Verification & scanning
│   │   ├── __init__.py
│   │   ├── scanner.py           # Bandit scanner integration
│   │   └── verifier.py          # Five-gate verification engine
│   ├── api/                      # External API clients
│   │   ├── __init__.py
│   │   ├── devin.py             # Devin API client
│   │   └── github.py            # GitHub API client
│   └── web/                      # Web interface
│       ├── __init__.py
│       └── web.py               # FastAPI application
├── tests/                        # Test suite
│   ├── test_db.py
│   ├── test_reconciler.py
│   ├── test_verifier.py
│   └── smoke.py
├── main.py                       # Application entry point
├── Dockerfile                    # Container definition
├── docker-compose.yml            # Container orchestration
└── requirements.txt              # Python dependencies
```

## Architecture

```
  scheduled scan  ─┐
  (or "scan now")  │   findings      ┌──────────────────────────────┐
                   ├────────────────►│         RECONCILER           │
  webhook: issue  ─┘                 │        every 30 seconds      │
  labeled auto                       │                              │
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


## Running the Workflow

The service runs as a single container handling both the reconciler loop and web interface.

### Option 1: Docker (Recommended)

**Prerequisites:**
- Docker and Docker Compose installed
- Devin API credentials and GitHub token
- Target repository checkout (e.g., `apache/superset` fork)

**Setup:**

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env with your real credentials:
# - DEVIN_API_KEY: Your Devin API key (service user with ManageOrgSessions)
# - DEVIN_ORG_ID: Your Devin organization ID
# - GITHUB_TOKEN: GitHub personal access token
# - GITHUB_REPO: Target repository in format "owner/repo"
# - DRY_RUN: Set to "false" for live mode

# 2. Configure target repository path
# Edit docker-compose.yml to set TARGET_REPO_HOST_PATH to your superset checkout
# Default: ../superset (relative to project directory)

# 3. Start the service
docker compose up
```

**Access the service:**
- Status page: http://localhost:8000/status
- Scan trigger: `curl -X POST http://localhost:8000/scan`
- Webhook endpoint: `http://localhost:8000/webhook` (for GitHub integration)

### Option 2: Local Development

**Prerequisites:**
- Python 3.11+
- Bandit 1.7.9 (`pip install bandit==1.7.9`)
- Target repository checkout

**Setup:**

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env with your credentials and set TARGET_REPO_PATH

# 3. Run the service
python main.py
```

**Access the service:**
- Status page: http://localhost:8000/status
- Scan trigger: `curl -X POST http://localhost:8000/scan`

### Workflow Steps

1. **Initial Scan**
   - Visit http://localhost:8000/status
   - Click "Scan Now" or use `curl -X POST http://localhost:8000/scan`
   - Bandit scans the target repository for security issues
   - Findings are synced to the database and GitHub issues are created

2. **Automated Processing**
   - The reconciler loop runs every 30 seconds (configurable via TICK_SECONDS)
   - Open issues labeled `cognition-project:auto` are picked up
   - Devin sessions are created to fix each finding
   - Sessions run with ACU limits and time constraints

3. **Verification**
   - When a session completes with a PR, the verifier runs
   - Five-gate verification checks: Join, Policy, Oracle, Tests, Publish
   - If verification passes, the task moves to READY state
   - If verification fails, the agent gets one repair attempt

4. **Human Review**
   - READY tasks are presented for human review
   - Review the PR and merge if acceptable
   - After merge, the task is marked MERGED and the issue closes

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
| `MAX_CONCURRENT` | Max concurrent sessions | `3` |
| `DAILY_ACU_CEILING` | Daily ACU spending limit | `100.0` |
| `RUN_TESTS_GATE` | Enable test verification gate | `false` |

### GitHub Webhook Integration (Optional)

For faster response to issue events, configure a GitHub webhook:

1. Go to your repository Settings → Webhooks
2. Add webhook: `http://your-server:8000/webhook`
3. Select events: Issues (or use "Send me everything")
4. The webhook triggers an immediate reconciler tick on issue changes

## The Verifier

The verifier runs as a subprocess with a hard timeout and answers one question: did the change the agent claims it made actually do what it says?

### Five Gates

| # | Gate | Checks |
|---|------|--------|
| 1 | **Join** | Footer fingerprint matches, `Fixes #<n>` resolves to the right issue, head SHA is current |
| 2 | **Policy** | Files ⊆ allowlist; diff ≤ 60 LOC; no `# nosec` / `# noqa` / `# type: ignore` added anywhere; no dependency-file changes |
| 3 | **Oracle** | Re-run the byte-identical pinned Bandit command: fingerprint absent **and** no new findings |
| 4 | **Tests** | The mapped test subset is green |
| 5 | **Publish** | Labels, evidence comment on the issue, ACUs recorded |

### What This Does Not Prove

The gate is not a proof of correctness. A scanner can be gamed by deleting the code path, or by restructuring something so Bandit stops flagging it while the underlying problem remains. The policy gate closes the obvious holes (suppression comments, oversized diffs, out-of-scope files); it does not close all of them.

**What the gate proves is narrow and specific: the thing the agent claimed happened, actually happened.** That is a meaningful reduction in what a human reviewer has to check, and it is not a substitute for the human reviewer.

**Merge is human, always.** When a human merges, the reconciler marks the task `MERGED` and closes the issue.

## Scale Metrics

At production volume, the numbers that matter are:

- **Human-reject rate on verified PRs** — The percentage of PRs that pass the verifier but are rejected by a human reviewer
- **Cost per merged PR** — Total ACU spend divided by number of merged PRs
- **Self-report accuracy as a trend** — The gap between what the agent claims and what the verifier finds, tracked over time

These metrics are defined but not built at n = 12. The current demo shows the data model and the claim→verdict rendering, but meaningful trends require production volume.

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