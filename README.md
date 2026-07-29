# cognition-project

Devin does the work, the verifier decides whether it worked, a human decides whether it ships.

## What this is

cognition-project is a small service that runs one closed loop against a fork of `apache/superset`:

**scan → file an issue → hand it to Devin → independently verify the resulting PR → either put it in front of a human, or send the evidence back to the same session for one repair attempt.**

The load-bearing word is *independently*. Devin reports what it did. The verifier decides whether that report is true, using deterministic checks the agent cannot reach or influence.

One container, one process, organized as a proper Python package, built in a day against the real Devin v3 API.

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
                │            │
                ▼            ▼
            BLOCKED     QUARANTINED
       (agent asked    (two verified
        a question)     rejections)
```

## Running the Service

The service runs as a single container handling both the reconciler loop and web interface.

### Prerequisites

- Docker and Docker Compose installed
- For live mode: Devin API credentials and GitHub token

### Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Required environment variables:

| Variable | Description |
|----------|-------------|
| `DEVIN_API_KEY` | Devin API key (service user with `ManageOrgSessions`) |
| `DEVIN_ORG_ID` | Devin organization ID |
| `GITHUB_TOKEN` | GitHub personal access token |
| `GITHUB_REPO` | GitHub repository in format `owner/repo` |

### Starting the Service

```bash
docker compose up
```

The status page will be available at http://localhost:8000/status

### Usage

- **Trigger a scan**: POST to `/scan` endpoint to run Bandit against the target repository
- **View status**: Visit `/status` for real-time task and fleet status
- **Webhook integration**: Configure GitHub webhook to POST to `/webhook` for issue-driven processing

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

### Running Locally

Install dependencies and run:

```bash
pip install -r requirements.txt
python main.py
```

The application will start the reconciler loop and web server, with the status page available at http://localhost:8000/status

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

### Running with Docker

Build and run with Docker Compose:

```bash
docker compose up
```

The status page will be available at http://localhost:8000/status