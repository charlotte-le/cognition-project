# Fixloop — Verified Autonomous Remediation for Apache Superset

> **Devin does the work. Fixloop decides whether it worked.**

---

## 1. What Fixloop is

Fixloop is a small service that runs one closed loop against a fork of `apache/superset`:

**scan → file an issue → hand it to Devin → independently verify the resulting PR → either put it in front of a human, or send the evidence back to the same session for one repair attempt.**

The load-bearing word is *independently*. Devin reports what it did. Fixloop's verifier decides whether that report is true, using deterministic checks the agent cannot reach or influence. A human decides whether it ships.

One container, one process, roughly 700 lines of Python, built in a day against the real Devin v3 API.

---

## 2. Why a trust layer, and not another trigger

Devin already ships the trigger half of this problem, and building it again would be pitching the platform's own roadmap back at it.

- **Automations** wire GitHub events, Slack messages, schedules, and custom webhooks to sessions, with per-session ACU limits, invocation rate caps, and an activity history. The template gallery includes scheduled vulnerability scans that open fix PRs.
- **Security Swarm** does scan → triage → remediate across repositories, with an org dashboard and a dedicated remediation endpoint.

So "an event becomes a Devin session" is a solved product problem. The unsolved one is the question immediately after it: **what has to be true before a team lets those sessions run unattended?**

Three things, and a trigger provides none of them:

1. **Independent verification.** Remediation guidance can *ask* an agent to prove its fix — write the failing test, re-run the scanner. That is a request. Fixloop runs the check itself, in its own container, against pinned tools, outside the session's reach. A prompt is a request; a gate is a guarantee.
2. **A durable task lifecycle.** One fingerprint → one issue → bounded attempts → quarantine, surviving restarts, duplicate events, and lost API responses, with a spend ceiling at every level.
3. **An evidence trail on the artifact itself**, including the gap between what the agent claimed and what the verifier found.

Fixloop is those three things. In a real engagement the trigger comes from Automations or the code-scans remediate endpoint, and Fixloop's verifier and ledger wrap them. Here the whole loop is built end to end because the seams are the point.

*(One practical note: GitHub-triggered Automations are restricted to private repositories. That is a concrete reason this project runs its own trigger against a public fork.)*

---

## 3. Why use an agent at all

The honest answer for the mechanical tier: you shouldn't. A codemod is cheaper and deterministic. Fixloop starts there anyway, because mechanical findings are trivially verifiable and therefore the cheapest possible place to prove the loop works.

Devin earns its cost on the second tier — read the surrounding code, understand why a hand-written test fails, change the implementation without touching the test, don't break the neighbors. There is no codemod for that.

The reusable asset is not the scanner. It is **the verification loop that makes progressively harder agent work safe to delegate.** Adding a new issue class costs a playbook and an oracle, not a new system.

---

## 4. The loop

```
   every 6 hours                      every 30 seconds
   ┌────────────────┐                 ┌──────────────────────────────┐
   │ pinned Bandit  │    findings     │         RECONCILER           │
   │ scan of fork   │────────────────►│                              │
   └────────────────┘                 │  desired : open labeled      │
                                      │            issues            │
   ┌────────────────┐                 │  observed: ledger ⋈ live     │
   │ GitHub issues  │────────────────►│            sessions ⋈ PRs    │
   │ labeled auto   │                 │  → one transition per task   │
   └────────────────┘                 └────┬────────────────────┬────┘
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

**The central design choice: react to state, not to events.** Every tick the reconciler re-derives what *should* be true (open labeled issues) against what *is* true (the ledger joined with live sessions and open PRs), and takes at most one step per task.

Nothing important lives in memory. A crash mid-flight costs thirty seconds, not a task. Duplicate signals are harmless because each transition is a conditional database update. This is why there is no webhook, no queue, and no retry framework in this system — **the loop is the retry framework.**

State machine, deliberately small:

```
PENDING ──► RUNNING ──► VERIFYING ──► READY ──► MERGED
                │            │
                ▼            ▼
            BLOCKED     QUARANTINED
       (agent asked    (two verified
        a question)     rejections)
```

---

## 5. One finding, end to end

This walkthrough is also the spine of the demo.

| Time | What happens |
|---|---|
| **T+0** | The 6-hour scheduled scan runs pinned Bandit against the fork. One finding: **B608**, SQL built by string construction, in `superset/connectors/sqla/models.py`. |
| **T+1s** | Fingerprint = `scan:` + `sha256(rule_id, normalized_path, code_context)[:8]`. Line numbers are deliberately excluded, so an unrelated edit above doesn't mint a "new" problem. `INSERT ... ON CONFLICT DO NOTHING` — re-scanning a known finding is a no-op by construction. |
| **T+3s** | A GitHub issue is created, labeled `fixloop:auto`, carrying a hidden `<!-- fixloop:fp=... -->` marker for a deterministic join later. |
| **T+30s** | Reconciler tick. A `PENDING` task with no session. Policy admits it: class allowlisted, daily ACU budget remaining, concurrency below 3. Session created with the rendered brief. State → `RUNNING`. |
| **T+30s → T+25m** | Each tick makes **one** bulk list call filtered on the `fixloop` tag and translates vendor status into the internal enum. A 45-minute wall clock runs locally; the ACU cap is enforced server-side. |
| **T+25m** | Status flips to `running` + `finished` — the completion signal. Harvest two *different* things: `structured_output` (what the agent **says** happened) and `pull_requests[]` (what the API **observed**). State → `VERIFYING`. |
| **T+25m** | Verifier, cheapest gate first. Join check passes. Policy gate: 12-line diff, files in the allowlist… **the diff adds `# nosec`.** Rejected immediately — note that the scanner itself would have passed. Suppressing the warning is exactly how a scanner-as-oracle system gets silently gamed. |
| **T+26m** | Fixloop messages **the same session** — it still has the repo cloned and its own reasoning, and messaging auto-resumes a suspended session — with the verifier's verbatim verdict: *"You added a suppression comment. That is not a fix. Parameterize the query."* Attempt 2 of a maximum 2. |
| **T+40m** | Second pass. Policy gate clears. The byte-identical pinned Bandit command shows the fingerprint gone and no new findings. The mapped test subset is green. **Pass.** |
| **T+41m** | Labels `fixloop:verified` and `ready-for-review`. An evidence comment lands on the issue: verdict, diff stats, session link, 7.2 ACUs. One advisory Devin Review is triggered on the PR. **A human merges** — Fixloop never merges anything. |
| **T+6h** | The next scheduled scan independently confirms the finding is absent from `main`. This is the one completion signal nothing inside the system can fake. |

The line the demo exists to deliver: **Devin's claim never becomes our truth. The verifier's verdict does.**

---

## 6. Division of labor

| Devin owns | Fixloop owns |
|---|---|
| Reading the code and understanding context | **Identity** — content-derived fingerprints, one per real defect |
| Writing the fix | **Admission** — is this class allowed, is there budget, is there capacity |
| Running its own checks | **Truth** — the deterministic verdict on whether it actually worked |
| Opening the PR | **Escalation** — blocked, rejected twice, or over budget |
| Asking a question when genuinely stuck | **Spend** — caps at session, concurrency, and daily level |
| Procedure (playbook), conventions (knowledge), environment (blueprint), credentials (secrets) | **Evidence** — the audit trail a leader reads without logging into anything new |

Everything in the left column is configured through the platform's own primitives rather than reimplemented in the orchestrator. Everything in the right column is what the platform deliberately leaves to the caller.

---

## 7. Issue classes and their oracles

Verifiability and agent-justification pull in opposite directions: the easier a task is to check, the less you needed an agent for it. Fixloop spans the range with two implemented classes.

| Tier | Class | Fingerprint | Oracle | Anti-gaming gate |
|---|---|---|---|---|
| **T1** | Mechanical scanner finding (Bandit) | `scan:` content hash | Re-run the byte-identical pinned scanner: fingerprint absent, no new findings | No `# nosec` / `# noqa` / `# type: ignore` anywhere in the diff; files ⊆ allowlist; diff ≤ 60 LOC; no dependency-file changes |
| **T2** | Behavioral fix, one curated issue | `issue:<n>` | **Human-authored differential test**: fails at base, must pass at head | The fix may not touch the test file; same policy gate |

### The T2 oracle is independent by construction

The tempting approach is to have the agent write the regression test and prove it fails at base and passes at head via mixed checkouts. Intellectually clean, and the single largest schedule risk in a one-day build.

The cheaper path gets the same guarantee: **the discriminating test is hand-authored and committed to the fork at base, at the moment the issue is filed.** It fails at base by construction, and that failing run is recorded in the issue as evidence. The verifier's job collapses to two clean checkouts — run the named test at base (must fail), run it at head (must pass) — plus a diff check that the test file is untouched. No git plumbing, no import ambiguity, and the oracle is independent because the agent never wrote it.

The T2 target is constrained to code covered by Superset's `tests/unit_tests/`, which runs on bare pytest with no external services.

**Disclosed in the README:** the T2 defect is seeded into the fork rather than discovered in the wild. Hunting for a real latent unit-testable bug is unbounded work, and the demo is about the verification loop, not the defect's provenance. The seeded defect is representative of its class, and calling that out plainly is cheaper than pretending otherwise.

---

## 8. Devin integration

Authentication is a service user credential (`cog_` prefix) with `ManageOrgSessions` at the organization scope. Confirming that round-trip is the first fifteen minutes of the build, because everything else depends on it.

### Creating a session

```jsonc
POST /v3/organizations/{org_id}/sessions
{
  "title": "[fixloop] scan:a91c3f2e att=1 — B608 sqla/models.py",
  "tags": ["fixloop"],                 // one fixed tag; orgs can enforce allowed-tag
                                       // lists, so correlation lives in the title
  "playbook_id": "...",                // the procedure
  "knowledge_ids": ["..."],            // Superset conventions
  "secret_ids": ["..."],               // fine-grained PAT: contents + PR write, fork only
  "repos": ["<me>/superset"],
  "max_acu_limit": 12,                 // 12 for T1, 20 for T2 — enforced server-side
  "bypass_approval": true,             // deliberate: safe mode parks the session at
                                       // waiting_for_approval with no error — a silent hang
  "structured_output_required": true,
  "structured_output_schema": { /* self-contained Draft-7, under 64KB */ },
  "prompt": "<rendered class brief>"   // the task
}
```

`prompt` is the only required field; everything else above is a deliberate choice.

**On idempotency.** The create schema has no idempotency parameter, so it is the caller's job. An `attempts` row with a client-generated request ID is written *before* the HTTP call, and the fingerprint and attempt number ride in the title. Each tick's bulk list surfaces any `fixloop`-tagged session the ledger doesn't recognize; those get logged and linked on the status page rather than silently duplicating work. This also covers the ordinary case of "the create succeeded but the network ate the response."

**Cost lever, unverified:** `devin_mode` accepts `lite` and `fast`, both behind feature-flag and preview restrictions. Lite may suit T1 well. It is named here as something to test, not something the design depends on.

### Monitoring — one bulk call per tick

A single list call filtered on `tags=fixloop` returns `status`, `status_detail`, `acus_consumed`, `pull_requests[]`, `structured_output`, and `title` for the entire fleet. Vendor statuses are normalized inside one module; raw values never reach the reconciler.

| Vendor `status` (+ `status_detail`) | Internal | Action |
|---|---|---|
| `new`, `claimed`, `resuming`, `running`+`working` | `RUNNING` | Wait. Wall clock running. |
| `running`+`waiting_for_user` | `BLOCKED` | Surface the question and session link on the status page. Stop the clock. |
| `running`+`finished` | `DONE` | Harvest output and PRs, then verify. **This is completion — not `exit`.** |
| `exit` (any detail) | `DONE` | Harvest whatever exists, then verify or fail. |
| `error` | `RUNNING` | Transient. Backoff and re-poll. Does **not** count against the attempt cap. |
| `suspended`+`inactivity` | `FAILED` | The platform is the no-progress clock. One nudge, then fail. |
| `suspended` + any billing/limit detail (`out_of_credits`, `out_of_quota`, `payment_declined`, `org_usage_limit_exceeded`, …) | `HALT` | Stop creating sessions entirely. Alert. Do not retry. |
| `running`+`waiting_for_approval` | `HALT` | Alert loudly — this one is *our* configuration bug. |

`structured_output` is used for **routing, never for truth**: `needs_human` routes to escalation; `scanner_clean: true` means we go run the scanner ourselves. The gap between the claim and the verdict is recorded per task and displayed.

### What the brief adds that the issue body doesn't

The issue says what is wrong. It says nothing about what proves it fixed. The brief adds:

1. **The exit criterion as an executable command** — the exact pinned Bandit invocation, or the named pytest node ID.
2. **Blast radius as prohibitions** — allowed paths, ≤ 60 changed lines, no suppression comments as the fix, no dependency-file changes, and for T2 no edits to the test file.
3. **The named test subset** to run before opening the PR.
4. **The branch and PR contract** — branch `fixloop/<fp>`, body containing `Fixes #<n>` plus a machine-readable footer.
5. **On attempt 2, the verifier's verbatim rejection.** Sessions share no memory across creates, so a retry is a *message to the same session*, never a new one.
6. **Environment facts** — push to the fork; upstream is read-only.
7. **The escalation contract** — don't guess; emit `needs_human` with one specific question and stop.
8. **The output schema restated in prose.**

### Platform features used rather than rebuilt

Sessions boot from a `.devin/blueprint.yaml` committed to the fork, so Superset is cloned and dependencies installed before the agent's first ACU is spent. Procedure lives in a playbook, conventions in knowledge, credentials in a secret. Every verified PR gets one advisory Devin Review — clearly labeled **advisory, not a gate**, because a non-deterministic reviewer belongs beside a deterministic one, not in front of it.

The scheduled scan runs locally rather than through the Schedules API for one reason: the scan must produce fingerprints the ledger owns *before* any session exists. In a customer deployment where findings arrive from Security Swarm or the code-scans endpoint, that inversion goes away.

---

## 9. Verification

The verifier runs as a **subprocess with a hard timeout** — it executes code from a branch an agent wrote, so a runaway test run kills a child process, not the orchestrator. Gates run cheapest-first; never spend a test run on a diff that already violates policy.

1. **Join check** — footer fingerprint matches, `Fixes #<n>` resolves to the right issue, head SHA is current.
2. **Policy gate** — files ⊆ allowlist; diff ≤ 60 LOC; no suppression comment added anywhere; no dependency-file changes; for T2, the test file is untouched.
3. **Class oracle** — T1: pinned scanner re-run, fingerprint absent, no new findings. T2: named test fails at base, passes at head.
4. **Mapped test subset green.**
5. **Publish** — labels, evidence comment on the issue, ACUs recorded, advisory review triggered.

On failure, the verbatim verdict goes back to the same session. **An attempt is a verified rejection** — infrastructure failures don't count against the cap. Two attempts and the task is quarantined: labeled `needs-human`, both diffs and both rejections posted, branch and PR left open as forensics, spending stopped.

**Merge is human, always.** When a human merges, the reconciler marks the task `MERGED` and closes the issue; the next scan independently confirms the finding is gone from `main`.

---

## 10. Cost control

Four limits, each answering "what stops this burning my budget":

| Limit | Mechanism |
|---|---|
| Per-session ACU cap | `max_acu_limit` at creation — enforced server-side, zero client code |
| Stall detection | The platform's own `suspended:inactivity` signal, not a local heuristic |
| Wall clock | 45 minutes local. On breach: send "stop and summarize what you changed and where you're stuck," wait 3 minutes, then terminate — which turns an opaque timeout into a diagnostic |
| Concurrency + daily ceiling | Concurrency is a `SELECT COUNT(*)` over active states, so it survives restart; start at 3. A global daily ACU ceiling halts new creation and shows on the status page |

---

## 11. Data model

Two tables, SQLite in WAL mode, one file volume. Verdicts live on the attempt, not the task.

```sql
tasks     fp PK, class, issue_number, state, attempt_count,
          acus_total, created_at, updated_at

attempts  id PK, fp FK, attempt_no, request_id, session_id,
          outcome, verdict_passed, gate_failed, evidence_json,
          started_at, ended_at              UNIQUE(fp, attempt_no)
```

Idempotency is the database's job. Inserts use `ON CONFLICT DO NOTHING`. Transitions are conditional updates — `UPDATE tasks SET state='RUNNING' WHERE fp=? AND state='PENDING'` — that proceed only when `rowcount == 1`. Cheap, restart-safe, and it needs no explanation beyond this paragraph.

---

## 12. Observability

**The primary artifact is the GitHub issue comment.** Session link → PR → verdict with evidence → cost. It lives where the work already lives, and nobody has to visit a new URL to audit a decision.

Behind it, one server-rendered HTML page showing counts, not rates — rates are fiction at n ≈ 12:

```
FIXLOOP                                        today: 41.3 / 100 ACU
──────────────────────────────────────────────────────────────────────
12 findings   7 verified   2 running   1 needs-human   2 quarantined

fp             class  state      att  ACU   claim → verdict        links
scan:a91c3f2e   T1    READY       2   7.2   fixed → REJECT → PASS  issue·PR·session
issue:47        T2    RUNNING     1   3.1   —                      issue·session
scan:c40b18d9   T1    QUARANTINE  2   9.8   fixed → REJECT ×2      issue·PR·session
```

The **claim → verdict** column is the whole thesis rendered as data: it shows, per task, exactly what trusting the agent's self-report would have cost. Org-wide session and ACU analytics already exist in the platform's own consumption and metrics endpoints; Fixloop doesn't rebuild them, it reports per-task cost and links out.

At production scale the numbers an engineering leader actually tracks are **human-reject rate on verified PRs** (the verifier's blind spot, and the number that decides whether the system survives contact with a real team), **cost per merged PR**, and **self-report accuracy as a trend**. Those are defined in the README as the scale-time metrics, not built at n = 12.

---

## 13. The build day

A session takes 15–60 minutes of wall clock, so the schedule is organized around one rule: **from hour two onward, a session is always running while I write code.**

| Hours | Work |
|---|---|
| **0–1** | Service user key; confirm create + list round-trip against the real API. Fork Superset, run pinned Bandit, pick 4–5 findings. Twenty-minute spike: bare `pip install` + `pytest` on one `tests/unit_tests/` module — this decides the T2 target. |
| **1–2** | Playbook, knowledge entry, and PAT secret created in the UI; IDs into `.env`. Commit `.devin/blueprint.yaml` to the fork. **Launch the first manual session and tune the prompt by hand** — iterating a prompt through custom orchestration at 25 minutes per take is how the day dies. |
| **2–6** | Ledger, scan → fingerprint → issue, session create / poll / harvest. |
| **6–9** | Verifier: policy gate, scanner re-run, test subset. Evidence comment, retry, quarantine. |
| **9–10** | Status page, `DRY_RUN` flag. |
| **10–14** | Run every finding through for real. Capture artifacts as they happen. Dump one fixtures file of real API responses so `SIMULATE=1` drives the same poll path with no credentials. |
| **Next morning** | README and Loom. |

**Cut order if the day runs long:** T2 → simulate replay → advisory review → status page. The issue comments are the primary artifact, so losing the page costs polish, not the story.

**One thing bought deliberately: rejection insurance.** The verifier catching a bad fix is the centerpiece of the demo, and an agent cannot be scheduled to misbehave on camera. So one run uses a deliberately weakened brief with the guardrail language removed, and is presented as exactly that — a disclosed control: *here is attempt one without the constraint, and here is what the gate caught.* That makes the point harder than an accident would, and it is honest to an audience that builds agents for a living. In practice the allowlist and diff-size gates trip organically far more often than suppression comments do, and any rejection powers the story equally well.

---

## 14. The Loom (5:00)

Nothing runs live end to end, so this is recorded around a pre-started run with cuts to saved artifacts.

- **0:00–0:30 — What.** The unbounded pile of small, real, individually-not-worth-a-sprint defects. Then, immediately: *"Devin already turns events into sessions — Automations and Security Swarm ship that. Fixloop is the layer you need before you trust those sessions unattended."*
- **0:30–3:30 — How.** Status page with a session mid-run → a completed task's evidence trail on the issue → the rejection: the agent claimed success, the verifier caught the suppression comment, the verbatim verdict went back to the same session, attempt two parameterized the query, the pinned scanner confirmed it. One sentence of architecture over the diagram, including: *"there's no webhook — the loop re-derives state every thirty seconds, so a crash costs thirty seconds."*
- **3:30–4:30 — Why Devin.** Not the mechanical tier — T2: a hand-authored failing test the agent had to genuinely fix without touching the test. Then the division-of-labor table, and the claim → verdict column as the measure of how much trust the gate is replacing.
- **4:30–5:00 — What's next.** Section 16.

Closing line: **"Devin's claim never becomes our truth. The verifier's verdict does."**

---

## 15. Deliberately not built

| Not built | Why |
|---|---|
| Webhook ingress, HMAC verification, delivery dedupe | The reconciler re-derives state every 30 seconds. A webhook would make it faster, not more correct, and it adds a tunnel and a security seam to a one-day build |
| Trigger plumbing that Automations already ship | At a customer, Automations or the code-scans remediate endpoint are the trigger; Fixloop wraps them |
| Agent-authored-test differential harness (mixed checkouts) | The hand-authored test gets the same independence guarantee with no git gymnastics. It is the documented next step |
| A third "correct refusal" issue class | Three field checks to verify, but 20+ minutes per prompt-iteration cycle. One paragraph in next steps |
| Local no-progress clock, soft ACU thresholds | The platform already emits `suspended:inactivity`, and the ACU cap is server-side |
| Event sourcing, an events table, a six-state vendor taxonomy | One asyncio loop's ticks are serialized. Conditional updates are kept because they are free; the essay is not |
| Prometheus, Grafana, a rates dashboard | Rates are fiction at n ≈ 12. Counters, the claim → verdict column, and one HTML page |
| LLM-as-judge verification | A non-deterministic oracle where deterministic ones exist. Devin Review is welcome as an advisory signal beside the gate, never as the gate |
| Auto-merge, auto-answering blocked sessions | Both remove the human gate that makes the system credible. Answers to recurring questions belong in the playbook, applied before the work starts |
| Semgrep, message brokers, Postgres, Celery, Kubernetes, a React frontend | Real operational cost, zero story value at roughly a dozen tasks a day |

---

## 16. In a real engagement

1. **Compose with the platform.** Triggers come from Automations or the enterprise code-scans remediate endpoint; Fixloop's verifier and ledger become the wrapper. Security Swarm findings arrive as another T1-shaped class with the same gates.
2. **Build the differential harness for agent-authored tests** — mixed-checkout fail-at-base / pass-at-head — so T2 scales past hand-authored oracles.
3. **Close the compounding loop.** Every confirmed false positive becomes a scanner baseline entry plus a knowledge note. Every question a session asks becomes a playbook amendment. The same information, applied before the work starts, so the success rate climbs without new code.
4. **Shadow mode for onboarding.** The `DRY_RUN` flag already does this: everything runs except the GitHub writes. It is what any serious customer will insist on for the first two weeks.
5. **Track the scale metrics.** Human-reject rate on verified PRs, cost per merged PR, and self-report accuracy as a trend line.

---

*Fixloop — Devin does the work; deterministic verification decides whether it worked; a human decides whether it ships.*
