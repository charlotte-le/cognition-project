# cognition-project — verified autonomous remediation for Apache Superset

> **Devin does the work. cognition-project decides whether it worked. A human decides whether it ships.**

---

## 1. What this is

cognition-project is a small service that runs one closed loop against a fork of `apache/superset`:

**scan → file an issue → hand it to Devin → independently verify the resulting PR → either put it in front of a human, or send the evidence back to the same session for one repair attempt.**

The load-bearing word is *independently*. Devin reports what it did. The verifier decides whether that report is true, using deterministic checks the agent cannot reach or influence.

One container, one process, roughly 400 lines of Python, built in a day against the real Devin v3 API.

---

## 2. Why a verifier, and not another trigger

Devin already ships the trigger half of this problem. Building it again would be pitching the platform's own roadmap back at it.

- **Automations** wire GitHub events, Slack messages, Linear tickets, schedules, and custom webhooks to sessions, with per-session ACU limits and an activity history. The template gallery includes scheduled vulnerability scans that open fix PRs.
- **Security Swarm** scans a codebase, validates each finding is exploitable at runtime in a sandbox, and ships remediation PRs.

So "an event becomes a Devin session" is solved. The unsolved question is the one immediately after it: **what has to be true before a team lets those sessions run unattended?**

### The distinction that matters

Security Swarm validates **the finding** — is this bug real and exploitable?

Nothing validates **the fix** — did the change the agent just wrote actually do what the agent says it did?

That is the gap this fills. Three things a trigger does not provide:

1. **Independent verification.** A prompt can *ask* an agent to prove its fix. That is a request. cognition-project runs the check itself, in its own container, against pinned tools, outside the session's reach. A prompt is a request; a gate is a guarantee.
2. **A durable task lifecycle.** One fingerprint → one issue → bounded attempts → quarantine. Survives restarts, duplicate events, and lost API responses, with a spend ceiling at every level.
3. **An evidence trail on the artifact itself**, including the gap between what the agent *claimed* and what the verifier *found*.

In a real engagement the trigger comes from Automations or Security Swarm, and this verifier and ledger wrap them. Here the whole loop is built end to end because the seams are the point.

*(Practical note: GitHub-triggered Automations only work on private repositories, for security reasons. That is the concrete reason this project runs its own trigger against a public fork.)*

---

## 3. Who does what

| Devin owns | cognition-project owns |
|---|---|
| Reading the code and understanding context | **Identity** — content-derived fingerprints, one per real defect |
| Writing the fix | **Admission** — is this class allowed, is there budget, is there capacity |
| Running its own checks | **Truth** — the deterministic verdict on whether it actually worked |
| Opening the PR | **Escalation** — blocked, rejected twice, or over budget |
| Asking a question when genuinely stuck | **Spend** — caps at session, concurrency, and daily level |
| Procedure, conventions, environment, credentials — via playbook, knowledge, blueprint, secrets | **Evidence** — the audit trail a leader reads without logging into anything new |

Everything on the left is configured through the platform's own primitives rather than reimplemented in the orchestrator. Everything on the right is what the platform deliberately leaves to the caller.

---

## 4. Why use an agent at all

Honest answer for the trivial cases: you shouldn't. A codemod is cheaper and deterministic.

The issue class here is **Bandit findings whose oracle is mechanical but whose fix is not** — for example `B608`, SQL built by string construction. Checking the fix is a scanner re-run. *Producing* the fix means reading the surrounding function, understanding where the values come from, parameterizing the query without changing behaviour, and not breaking the neighbours. There is no codemod for that.

That separation is the whole point:

- **Cheap to verify** — so the loop can be trusted.
- **Expensive to produce** — so the agent earns its cost.

The reusable asset is not the scanner. It is **the verification loop that makes progressively harder agent work safe to delegate.** Adding a new issue class costs a playbook and an oracle, not a new system.

---

## 5. How it works

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

**The central design choice: react to state, not to events.**

Every 30 seconds the reconciler re-derives what *should* be true (open labeled issues) against what *is* true (the ledger joined with live sessions and open PRs), and takes at most one step per task.

Nothing important lives in memory. A crash mid-flight costs thirty seconds, not a task. Duplicate signals are harmless because each transition is a conditional database update.

**This is why the webhook is six lines.** It doesn't parse a payload, verify a signature, or deduplicate deliveries. It sets a flag that makes the next tick run immediately. The loop is already idempotent, so the webhook is allowed to be dumb. It makes the system faster, not more correct — and if it breaks, the 30-second tick catches everything anyway.

### State machine

```
PENDING ──► RUNNING ──► VERIFYING ──► READY ──► MERGED
                │            │
                ▼            ▼
            BLOCKED     QUARANTINED
       (agent asked    (two verified
        a question)     rejections)
```

---

## 6. One finding, end to end

This walkthrough is also the spine of the demo.

| Time | What happens |
|---|---|
| **T+0** | Pinned Bandit runs against the fork. One finding: **B608**, SQL built by string construction, in `superset/connectors/sqla/models.py`. |
| **T+1s** | Fingerprint = `scan:` + `sha256(rule_id, normalized_path, code_context)[:8]`. Line numbers are deliberately excluded, so an unrelated edit fifty lines above doesn't mint a "new" problem. `INSERT ... ON CONFLICT DO NOTHING` — re-scanning a known finding is a no-op by construction. |
| **T+3s** | A GitHub issue is created, labeled `cognition-project:auto`, carrying a hidden `<!-- cognition-project:fp=... -->` marker for a deterministic join later. The label event hits the webhook; the next tick runs immediately. |
| **T+5s** | Reconciler tick. A `PENDING` task with no session. Policy admits it: class allowlisted, daily ACU budget remaining, concurrency below 3. Session created with the rendered brief. State → `RUNNING`. |
| **T+5s → T+25m** | Each tick makes one list call filtered on the `cognition-project` tag and translates vendor status into the internal enum. A 45-minute wall clock runs locally; the ACU cap is enforced server-side. |
| **T+25m** | Status flips to `running` + `finished` — the completion signal. Harvest two *different* things: `structured_output` (what the agent **says** happened) and `pull_requests[]` (what the API **observed**). State → `VERIFYING`. |
| **T+25m** | Verifier, cheapest gate first. Join check passes. Policy gate: 12-line diff, files in the allowlist… **the diff adds `# nosec`.** Rejected immediately — note that the scanner itself would have passed. Suppressing the warning is exactly how a scanner-as-oracle system gets silently gamed. |
| **T+26m** | The orchestrator messages **the same session** — it still has the repo cloned and its own reasoning, and messaging auto-resumes a suspended session — with the verifier's verbatim verdict: *"You added a suppression comment. That is not a fix. Parameterize the query."* Attempt 2 of a maximum 2. |
| **T+40m** | Second pass. Policy gate clears. The byte-identical pinned Bandit command shows the fingerprint gone and no new findings. The mapped test subset is green. **Pass.** |
| **T+41m** | Labels `cognition-project:verified` and `ready-for-review`. An evidence comment lands on the issue: verdict, diff stats, session link, 7.2 ACUs. **A human merges** — the system never merges anything. |
| **Next scan** | Independently confirms the finding is absent from `main`. This is the one completion signal nothing inside the system can fake. |

The line the demo exists to deliver: **Devin's claim never becomes our truth. The verifier's verdict does.**

---

## 7. The verifier

Runs as a **subprocess with a hard timeout** — it executes code from a branch an agent wrote, so a runaway test run kills a child process, not the orchestrator. Gates run cheapest-first; never spend a test run on a diff that already violates policy.

| # | Gate | Checks |
|---|---|---|
| 1 | **Join** | Footer fingerprint matches, `Fixes #<n>` resolves to the right issue, head SHA is current |
| 2 | **Policy** | Files ⊆ allowlist; diff ≤ 60 LOC; no `# nosec` / `# noqa` / `# type: ignore` added anywhere; no dependency-file changes |
| 3 | **Oracle** | Re-run the byte-identical pinned Bandit command: fingerprint absent **and** no new findings |
| 4 | **Tests** | The mapped test subset is green |
| 5 | **Publish** | Labels, evidence comment on the issue, ACUs recorded |

On failure the verbatim verdict goes back to the same session, unedited. **An attempt is a verified rejection** — infrastructure failures don't count against the cap. Two attempts and the task is quarantined: labeled `needs-human`, both diffs and both rejections posted, branch and PR left open as forensics, spending stopped.

### What this does not prove — stated up front

The gate is not a proof of correctness. A scanner can be gamed by deleting the code path, or by restructuring something so Bandit stops flagging it while the underlying problem remains. The policy gate closes the obvious holes (suppression comments, oversized diffs, out-of-scope files); it does not close all of them.

**What the gate proves is narrow and specific: the thing the agent claimed happened, actually happened.** That is a meaningful reduction in what a human reviewer has to check, and it is not a substitute for the human reviewer.

**Merge is human, always.** When a human merges, the reconciler marks the task `MERGED` and closes the issue.

---

## 8. What we use from the Devin platform

Authentication is a service user credential (`cog_` prefix) with `ManageOrgSessions` at organization scope.

### Creating a session

```jsonc
POST /v3/organizations/{org_id}/sessions
{
  "prompt": "<rendered class brief>",   // the only required field
  "title": "[cognition-project] scan:a91c3f2e att=1 — B608 sqla/models.py",
  "tags": ["cognition-project"],        // correlation key for the fleet list
  "repos": ["<me>/superset"],
  "playbook_id": "...",                 // the procedure
  "knowledge_ids": ["..."],             // Superset conventions
  "session_secrets": [                  // session-scoped GitHub PAT: contents +
    { "key": "GH_TOKEN",                // PR write, fork only. Dies with the
      "value": "...",                   // session; never stored org-wide.
      "sensitive": true }
  ],
  "max_acu_limit": 12,                  // enforced server-side
  "bypass_approval": true,              // deliberate — see below
  "structured_output_required": true,
  "structured_output_schema": { /* self-contained Draft-7 */ }
}
```

Everything except `prompt` is a deliberate choice:

- **`bypass_approval: true`** — without it, safe mode parks the session at `waiting_for_approval` with no error and no ACU burn. It looks exactly like a slow session for forty minutes. This is the kind of thing you only learn by running it.
- **`session_secrets`** — the PAT lives for the life of the session and is never written to org secrets. Strictly better security posture than a persistent org secret, and one fewer object to provision.
- **`max_acu_limit`** — a server-side spend cap, zero client code.
- **`structured_output_schema`** — this is how the agent makes its *claim* in a machine-readable form. Without it there is nothing to compare the verdict against.

**Idempotency.** Create has no idempotency parameter, so it's the caller's job. An `attempts` row with a client-generated request ID is written *before* the HTTP call, and the fingerprint and attempt number ride in the title. Each tick, any tagged session the ledger doesn't recognize gets logged and surfaced on the status page rather than silently duplicating work. This covers the ordinary case of "the create succeeded but the network ate the response."

### Monitoring — the insights list endpoint

One call per tick returns, for every tagged session:

`status`, `status_detail`, `acus_consumed`, `pull_requests[{pr_state, pr_url}]`, `structured_output`, `title`, `url` — plus an `analysis` block containing `note_usage` (which knowledge notes helped and which didn't), `suggested_prompt` with feedback items, `issues`, and a `timeline`.

That is most of the observability layer for free. It also does something better: **`note_usage.bad_usages` and `suggested_prompt.feedback_items` are the platform telling us where our own brief was underspecified.** That feedback goes straight back into the playbook. See §11.

Note: `structured_output` and `status_detail` are only populated on get/list responses, never on create. A session's real state is unknowable until the first poll.

Vendor statuses are normalized inside one module; raw values never reach the reconciler.

| Vendor `status` (+ `status_detail`) | Internal | Action |
|---|---|---|
| `new`, `claimed`, `resuming`, `running`+`working` | `RUNNING` | Wait. Wall clock running. |
| `running`+`waiting_for_user` | `BLOCKED` | Surface the question and session link. Stop the clock. |
| `running`+`finished` | `DONE` | Harvest and verify. **This is completion — not `exit`.** |
| `exit` (any detail) | `DONE` | Harvest whatever exists, then verify or fail. |
| `error` | `RUNNING` | Transient. Backoff. Does **not** count against the attempt cap. |
| `suspended`+`inactivity` | `FAILED` | The platform is the no-progress clock. |
| `suspended` + any billing/limit detail | `HALT` | Stop creating sessions entirely. Alert. Do not retry. |
| `running`+`waiting_for_approval` | `HALT` | Alert loudly — this one is *our* configuration bug. |

`structured_output` is used for **routing, never for truth**: `needs_human` routes to escalation; `scanner_clean: true` means we go run the scanner ourselves. The gap between the claim and the verdict is recorded per task and displayed.

### What the brief adds that the issue body doesn't

The issue says what is wrong. It says nothing about what proves it fixed. The brief adds:

1. **The exit criterion as an executable command** — the exact pinned Bandit invocation.
2. **Blast radius as prohibitions** — allowed paths, ≤ 60 changed lines, no suppression comments as the fix, no dependency-file changes.
3. **The named test subset** to run before opening the PR.
4. **The branch and PR contract** — branch `cognition-project/<fp>`, body containing `Fixes #<n>` plus a machine-readable footer.
5. **On attempt 2, the verifier's verbatim rejection.** Sessions share no memory across creates, so a retry is a *message to the same session*, never a new one.
6. **The escalation contract** — don't guess; emit `needs_human` with one specific question and stop.

### Configured, not coded

- **Environment** — a YAML blueprint produces the snapshot every session boots from, so Superset is cloned and dependencies installed before the agent's first ACU is spent. Devin generates the blueprint itself when asked to configure the repo; we reviewed and approved it rather than writing it.
- **Advisory Devin Review** — auto-review is a toggle in Settings > Review. Every PR gets one, clearly labeled **advisory, not a gate**. A non-deterministic reviewer belongs beside a deterministic one, not in front of it. Cost: one checkbox.

---

## 9. Cost control

Four limits, each answering "what stops this burning my budget":

| Limit | Mechanism |
|---|---|
| Per-session ACU cap | `max_acu_limit` at creation — enforced server-side, zero client code |
| Stall detection | The platform's own `suspended:inactivity` signal, not a local heuristic |
| Wall clock | 45 minutes local. On breach: log, mark `FAILED`, stop polling |
| Concurrency + daily ceiling | Concurrency is a `SELECT COUNT(*)` over active states, so it survives restart; start at 3. A global daily ACU ceiling halts new creation and shows on the status page |

---

## 10. Data model and observability

### Two tables, SQLite in WAL mode, one file volume

```sql
tasks     fp PK, class, issue_number, state, attempt_count,
          acus_total, created_at, updated_at

attempts  id PK, fp FK, attempt_no, request_id, session_id,
          outcome, verdict_passed, gate_failed, evidence_json,
          started_at, ended_at              UNIQUE(fp, attempt_no)
```

Verdicts live on the attempt, not the task. Inserts use `ON CONFLICT DO NOTHING`. Transitions are conditional updates — `UPDATE tasks SET state='RUNNING' WHERE fp=? AND state='PENDING'` — that proceed only when `rowcount == 1`. Cheap, restart-safe, and it needs no explanation beyond this paragraph.

### The primary artifact is the GitHub issue comment

Session link → PR → verdict with evidence → cost. It lives where the work already lives, and nobody has to visit a new URL to audit a decision.

### One status page

Server-rendered, no JS. Counts, not rates — rates are fiction at n ≈ 12.

```
COGNITION-PROJECT                              today: 41.3 / 100 ACU
──────────────────────────────────────────────────────────────────────
12 findings   7 verified   2 running   1 needs-human   2 quarantined

fp             state      att  ACU   claim → verdict        links
scan:a91c3f2e  READY       2   7.2   fixed → REJECT → PASS  issue·PR·session
scan:6b21f0aa  RUNNING     1   3.1   —                      issue·session
scan:c40b18d9  QUARANTINE  2   9.8   fixed → REJECT ×2      issue·PR·session
```

The **claim → verdict** column is the whole thesis rendered as data. It shows, per task, exactly what trusting the agent's self-report would have cost.

Org-wide session and ACU analytics already exist in the platform's consumption and metrics endpoints. cognition-project doesn't rebuild them; it reports per-task cost and links out.

At production scale, the numbers an engineering leader actually tracks are **human-reject rate on verified PRs** (the verifier's blind spot, and the number that decides whether the system survives contact with a real team), **cost per merged PR**, and **self-report accuracy as a trend**. Those are defined in the README as the scale-time metrics, not built at n = 12.

---

## 11. In a real engagement

1. **Compose with the platform.** Triggers come from Automations or Security Swarm; this verifier and ledger become the wrapper. Swarm findings arrive as another class with the same gates. Swarm proves the bug is real; cognition-project proves the fix is real.
2. **Add a behavioural tier.** A differential test harness — mixed checkouts, fail-at-base / pass-at-head — extends the loop to bugs a scanner can't express. This is the natural next class and the one that most increases what's safe to delegate.
3. **Close the compounding loop, using data the platform already returns.** The insights endpoint reports which knowledge notes helped and which didn't, and suggests prompt improvements. Every confirmed false positive becomes a scanner baseline entry. Every question a session asks becomes a playbook amendment. The same information, applied *before* the work starts, so the success rate climbs without new code.
4. **Shadow mode for onboarding.** The `DRY_RUN` flag already does this: everything runs except the GitHub writes. It is what any serious customer will insist on for the first two weeks.
5. **Track the scale metrics.** Human-reject rate on verified PRs, cost per merged PR, self-report accuracy as a trend line.

---

## 12. Deliberately not built

| Not built | Why |
|---|---|
| A behavioural / differential-test tier | The highest-value extension, and 3–4 hours of environment wrangling for 30 seconds of demo. Documented as next step #2 rather than half-built |
| HMAC verification, delivery dedupe, a queue | The reconciler re-derives state every 30 seconds. The webhook is a latency optimization; correctness doesn't depend on it |
| Trigger plumbing that Automations already ship | At a customer, Automations or Security Swarm are the trigger; this wraps them |
| A bootstrap script that provisions playbook/knowledge via API | Real endpoints, but nobody re-runs it. The playbook and knowledge *text* are committed to the repo instead — that's the part worth reading |
| A fixture-replay harness | A second code path to maintain, serving a reviewer who has a Devin org anyway. Instead: real recorded responses in `fixtures/` and a pre-populated database so the status page renders with no credentials |
| Local no-progress clocks, wall-clock negotiation protocols | The platform already emits `suspended:inactivity`, and the ACU cap is server-side |
| Event sourcing, an events table | One asyncio loop's ticks are serialized. Conditional updates are kept because they're free; the essay is not |
| Prometheus, Grafana, a rates dashboard | Rates are fiction at n ≈ 12. Counters, the claim → verdict column, one HTML page |
| LLM-as-judge verification | A non-deterministic oracle where deterministic ones exist. Devin Review is welcome as an advisory signal beside the gate, never as the gate |
| Auto-merge, auto-answering blocked sessions | Both remove the human gate that makes the system credible. Answers to recurring questions belong in the playbook, applied before the work starts |
| Semgrep, message brokers, Postgres, Celery, Kubernetes, a React frontend | Real operational cost, zero story value at roughly a dozen tasks a day |

---

## 13. The Loom (5:00)

- **0:00–0:15 — The artifact, no narration.** A GitHub issue showing `REJECT → verbatim verdict → PASS`. *"This is what the system produced. Here's how."*
- **0:15–0:45 — What and why.** The unbounded pile of small, real, individually-not-worth-a-sprint defects. Then immediately: *"Devin already turns events into sessions — Automations and Security Swarm ship that. Swarm proves the bug is real. Nothing proves the fix is real. That's what this is."*
- **0:45–1:00 — Live.** Add the `cognition-project:auto` label to an issue on camera. Five seconds later the status page shows a new session with a working Devin link.
- **1:00–3:30 — How.** Status page with a session mid-run → a completed task's evidence trail on the issue → the rejection: the agent claimed success, the verifier caught the suppression comment, the verbatim verdict went back to the *same session*, attempt two parameterized the query, the pinned scanner confirmed it. One sentence of architecture over the diagram: *"there's no queue — the loop re-derives state every thirty seconds, so a crash costs thirty seconds, and the webhook is six lines because it's allowed to be dumb."*
- **3:30–4:30 — Why Devin.** The diff. Cheap to verify, expensive to produce — no codemod parameterizes that query. The division-of-labor table. The claim → verdict column as the measure of how much trust the gate is replacing. And the honest limit: *the gate proves the claimed thing happened, not that the code is correct. Merge is human.*
- **4:30–5:00 — What's next.** §11, with the insights-endpoint feedback loop on screen.

Closing line: **"Devin's claim never becomes our truth. The verifier's verdict does."**

---

*cognition-project — Devin does the work; deterministic verification decides whether it worked; a human decides whether it ships.*
