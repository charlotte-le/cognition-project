# cognition-project

Devin does the work, the verifier decides whether it worked, a human decides whether it ships.

## What this is

cognition-project is a small service that runs one closed loop against a fork of `apache/superset`:

**scan → file an issue → hand it to Devin → independently verify the resulting PR → either put it in front of a human, or send the evidence back to the same session for one repair attempt.**

The load-bearing word is *independently*. Devin reports what it did. The verifier decides whether that report is true, using deterministic checks the agent cannot reach or influence.

One container, one process, roughly 400 lines of Python, built in a day against the real Devin v3 API.

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

## Run Modes

Both modes use `docker compose up`.

### Demo Mode (No credentials)

When no `.env` file is present, the system runs in demo mode:
- Pre-seeded database with realistic sample data
- Status page renders immediately at http://localhost:8000/status
- No live API calls to Devin or GitHub
- Perfect for demonstrating the interface and data model

### Live Mode

With real credentials in `.env`:
- Fill in `.env` with your Devin API key, org ID, and GitHub token
- POST /scan to trigger a Bandit scan
- Watch the reconciler loop create sessions, verify PRs, and manage the task lifecycle
- Real integration with Devin v3 API and GitHub

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DEVIN_API_BASE` | Devin API base URL | `https://api.devin.ai` |
| `DEVIN_API_KEY` | Devin API key (service user with `ManageOrgSessions`) | (empty) |
| `DEVIN_ORG_ID` | Devin organization ID | (empty) |
| `GITHUB_REPO` | GitHub repository in format `owner/repo` | `owner/repo` |
| `GITHUB_TOKEN` | GitHub personal access token | (empty) |
| `DRY_RUN` | Dry run mode - skip actual API calls | `true` |

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

Run locally without Docker:

```bash
pip install -r requirements.txt
python main.py
```

Run with Docker:

```bash
docker compose up
```

The status page will be available at http://localhost:8000/status