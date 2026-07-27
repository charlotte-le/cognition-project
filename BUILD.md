# BUILD.md — implementing Fixloop

Companion to `DESIGN.md`. That document argues for the system; this one builds it.

Ordered so that **from Step 4 onward a Devin session is always running while you write code**. Total ≈ 14 hours plus a morning for README and Loom.

Each step ends with a **Done when** line. If a step isn't done, don't start the next one — the later steps all assume the earlier contracts hold.

---

## 0. Verified API facts

Checked against the live v3 docs. The endpoint surface in `DESIGN.md` is accurate. Three things to know before you write code:

**`status_detail` and `structured_output` are only populated on get/list responses.** They are absent from the create response. The state machine keys entirely off `status` + `status_detail`, so a session's real state is unknowable until the first poll. Treat create as returning only `session_id`, `url`, and a provisional `status`.

**The list endpoint declares `qs` as a required, object-typed query parameter.** Whether `?tags=fixloop&first=100` flattens correctly is the single unverified assumption in the design. Resolve it in Step 1. Fallback is per-session `GET /sessions/{id}`, which at n ≈ 12 costs 12 calls per tick and is trivially correct — the bulk call is an optimization, not a dependency.

**Playbooks, knowledge, and secrets all have create endpoints.** Build them in `bootstrap.py` rather than clicking through the UI:

| Object | Endpoint | Body | Returns |
|---|---|---|---|
| Playbook | `POST /v3/organizations/{org}/playbooks` | `{name, instructions}` | `playbook_id` |
| Knowledge | `POST /v3/organizations/{org}/knowledge/notes` | `{name, trigger, body}` | `note_id` |
| Secret | `POST /v3/organizations/{org}/secrets` | — | secret id |

A grader who can re-run your bootstrap is worth more than the ten minutes of clicking you saved.

Also confirmed: `devin_mode` accepts `normal`, `fast`, and `lite`. `lite` is a real enum value, still behind preview restrictions. Test it on T1 if there's time; don't design around it.

---

## 1. Prove the round-trip — 30 min

Nothing else matters if this doesn't work.

```bash
export DEVIN_API_KEY="cog_..."          # service user, ManageOrgSessions at org scope
export DEVIN_ORG_ID="org-..."

curl "https://api.devin.ai/v3/self" -H "Authorization: Bearer $DEVIN_API_KEY"
```

Then create a throwaway session with nothing but a prompt, list it back, and print the raw JSON both times.

Resolve the `qs` question here — try the flattened form first, fall back to per-session GET, and write down which one you're using.

**Save every response body to `fixtures/`.** This is your `SIMULATE=1` corpus and you get it for free by not throwing the JSON away.

**Done when:** you have a session ID, its raw list-response JSON on disk, and a decided polling strategy.

---

## 2. Fork and scan — 30 min

Fork `apache/superset` into your own org.

Pin the scanner and record the exact invocation as a module-level constant. Byte-identical re-runs are the entire T1 oracle — a scanner version drift between the finding scan and the verification scan silently invalidates every verdict.

```python
BANDIT_CMD = ["bandit", "-r", "superset/", "-f", "json", "-ll"]
BANDIT_VERSION = "1.7.9"   # pinned in requirements.txt AND asserted at runtime
```

Pick 4–5 findings across at least two rule IDs.

**Then the T2 spike, timeboxed to twenty minutes:** bare `pip install`, run one module under `tests/unit_tests/` on plain pytest, no external services. If it isn't green in twenty minutes, **cut T2 now**. Cutting it at hour nine costs you the verifier.

**Done when:** fork exists, `bandit` output is saved to `fixtures/scan_baseline.json`, and T2 is either confirmed viable with a named target module or formally cut.

---

## 3. Bootstrap the platform objects — 45 min

Write `bootstrap.py`. It creates the playbook, knowledge note, and PAT secret via API, prints the IDs, and you paste them into `.env`. Make it idempotent — list first, create only if absent.

Commit `.devin/blueprint.yaml` to the fork so sessions boot with Superset cloned and dependencies installed before the first ACU is spent.

The GitHub PAT stored as the Devin secret should be fine-grained: contents + pull-request write, **scoped to your fork only**. Upstream stays read-only.

Set `bypass_approval: true` on session creation. Safe mode parks the session at `waiting_for_approval` with no error and no ACU burn — a silent hang that looks exactly like a slow session for forty minutes.

**Done when:** `.env` has real IDs for playbook, knowledge, and secret, and `blueprint.yaml` is on the fork's default branch.

---

## 4. Hand-tune one prompt — 45 min

Launch a session **manually in the UI** against one real T1 finding, using the playbook and knowledge you just created. Iterate the brief by hand until it reliably produces:

- branch `fixloop/<fp>`
- PR body containing `Fixes #<n>` and the machine-readable footer
- valid structured output against your schema

Iterating a prompt through your own orchestrator at 25 minutes per take is how the day dies. Get the prompt right while the orchestrator doesn't exist yet.

**Done when:** one manual session has produced a conforming PR, and the brief text is saved to `prompts/t1.md`.

> From here on: whenever you're about to write code for 30+ minutes, kick off a session first.

---

## 5. Ledger and issue creation — 2 hrs

Two tables, SQLite in WAL mode, one file on a volume.

```sql
CREATE TABLE tasks (
  fp TEXT PRIMARY KEY, class TEXT, issue_number INTEGER,
  state TEXT, attempt_count INTEGER DEFAULT 0, acus_total REAL DEFAULT 0,
  created_at INTEGER, updated_at INTEGER
);

CREATE TABLE attempts (
  id INTEGER PRIMARY KEY, fp TEXT REFERENCES tasks(fp), attempt_no INTEGER,
  request_id TEXT, session_id TEXT, outcome TEXT,
  verdict_passed INTEGER, gate_failed TEXT, evidence_json TEXT,
  started_at INTEGER, ended_at INTEGER,
  UNIQUE(fp, attempt_no)
);
```

Two invariants, and they carry the whole restart story:

- Inserts use `INSERT ... ON CONFLICT DO NOTHING`.
- **Every** transition is a conditional update, and you check the rowcount:

```python
cur = db.execute(
    "UPDATE tasks SET state='RUNNING', updated_at=? WHERE fp=? AND state='PENDING'",
    (now, fp))
if cur.rowcount != 1:
    return          # someone else moved it, or it wasn't where we thought. Not an error.
```

Then `scan.py`: findings → fingerprints → GitHub issues.

```python
fp = "scan:" + sha256(f"{rule_id}|{normalized_path}|{code_context}").hexdigest()[:8]
```

Line numbers are deliberately excluded. An unrelated edit fifty lines above must not mint a new task.

Issue body carries the hidden marker `<!-- fixloop:fp=scan:a91c3f2e -->` and the label `fixloop:auto`.

**Done when:** running the scanner twice creates issues the first time and does nothing the second time.

---

## 6. Session lifecycle — 2 hrs

`devin.py` exposes exactly three operations — `create`, `list_fleet`, `message` — plus one normalization function. **Raw vendor status strings never leave this module.**

Write the `attempts` row with a client-generated `request_id` *before* the HTTP call, and put the fingerprint and attempt number in the session title:

```
[fixloop] scan:a91c3f2e att=1 — B608 sqla/models.py
```

There's no idempotency parameter on create, so this is your recovery path when the create succeeds and the network eats the response. Each tick, any `fixloop`-tagged session the ledger doesn't recognize gets logged and surfaced on the status page rather than silently duplicating work.

Normalization table — implement this literally as a dict, and unit test it:

| Vendor `status` (+ detail) | Internal | Action |
|---|---|---|
| `new`, `claimed`, `resuming`, `running`+`working` | `RUNNING` | Wait. Wall clock runs. |
| `running`+`waiting_for_user` | `BLOCKED` | Surface question + link. Stop the clock. |
| `running`+`finished` | `DONE` | **This is completion.** Harvest, then verify. |
| `exit` (any detail) | `DONE` | Harvest whatever exists, verify or fail. |
| `error` | `RUNNING` | Transient. Backoff. Does *not* count as an attempt. |
| `suspended`+`inactivity` | `FAILED` | One nudge, then fail. |
| `suspended`+ billing/limit detail | `HALT` | Stop all creation. Alert. No retry. |
| `running`+`waiting_for_approval` | `HALT` | Alert loudly — this is *our* config bug. |

Billing details to match: `out_of_credits`, `out_of_quota`, `no_quota_allocation`, `payment_declined`, `usage_limit_exceeded`, `org_usage_limit_exceeded`, `total_session_limit_exceeded`.

On harvest, take two separate things: `structured_output` (what the agent **says**) and `pull_requests[]` (what the API **observed**). Store both. The gap between them is the demo.

**Done when:** a real session goes `PENDING → RUNNING → VERIFYING` from a reconciler tick, with no manual intervention.

---

## 7. Verifier — 3 hrs

The centerpiece. Give it the time; cut elsewhere.

Runs as a **subprocess with a hard timeout** — you are executing code an agent wrote, and a runaway test run should kill a child process, not the orchestrator.

```python
subprocess.run(cmd, timeout=600, capture_output=True, cwd=checkout_dir)
```

Gates cheapest-first. Never spend a test run on a diff that already violates policy.

1. **Join check** — footer fingerprint matches, `Fixes #<n>` resolves to the right issue, head SHA is current.
2. **Policy gate** — files ⊆ allowlist; diff ≤ 60 LOC; no `# nosec` / `# noqa` / `# type: ignore` added anywhere; no dependency-file changes; for T2, the test file is untouched.
3. **Class oracle** — T1: re-run the byte-identical pinned scanner; fingerprint absent AND no new findings. T2: named test fails at base, passes at head (two clean checkouts, no git plumbing).
4. **Mapped test subset green.**

Return a structured verdict: `passed`, `gate_failed`, and a **verbatim human-readable rejection string**. That string goes back to the agent unedited in Step 8 — don't summarize it, don't template over it.

**Done when:** a known-good PR passes and a hand-crafted `# nosec` PR is rejected at gate 2 without ever running the scanner.

---

## 8. Publish, retry, quarantine — 1 hr

On pass: labels `fixloop:verified` + `ready-for-review`, evidence comment on the issue (verdict, diff stats, session link, ACUs), trigger one advisory Devin Review on the PR. **Never merge.**

On fail: `message` the **same session** with the verbatim verdict. It still has the repo cloned and its own reasoning, and messaging auto-resumes a suspended session. Never create a new session for a retry — sessions share no memory across creates.

An attempt is a **verified rejection**. Infrastructure failures don't count against the cap. Two attempts → quarantine: label `needs-human`, post both diffs and both rejections, leave branch and PR open as forensics, stop spending.

When a human merges, the reconciler marks `MERGED` and closes the issue.

**Done when:** you have one task that was rejected, messaged, and passed on attempt two — with both verdicts visible on the issue.

---

## 9. Status page and flags — 1 hr

Flask plus a daemon thread running the reconciler loop is the simplest thing that works and keeps ticks serialized. One server-rendered page, no JS, no polling endpoint.

Counters only — rates are fiction at n ≈ 12. The **claim → verdict** column is the thesis rendered as data; make sure it reads clearly at a glance.

Two flags:

- `DRY_RUN=1` — everything runs except GitHub writes. This is also your shadow-mode story for Section 16.
- `SIMULATE=1` — replay `fixtures/` through the same poll path, no credentials. This is how a grader evaluates you without a Devin org.

**Done when:** `SIMULATE=1 docker compose up` renders a populated status page on a machine with no API key.

---

## 10. Run it for real — 4 hrs

Every finding, end to end.

**Screenshot as you go.** A mid-run status page with a session in flight cannot be reconstructed afterward, and it's the first thing on screen in the Loom.

Include the disclosed control run: one session with the guardrail language stripped from the brief, presented in the video as exactly that. In practice the allowlist and diff-size gates trip organically far more often than suppression comments — any rejection powers the story equally well, so take whichever one you actually get.

**Done when:** ≥ 1 verified-and-merged task, ≥ 1 rejection-then-pass, ≥ 1 quarantine, all with artifacts captured.

---

## 11. Docker, README, Loom — 1 hr + a morning

Single `Dockerfile`, one volume for the SQLite file, `docker compose up`.

README must cover:

- Quickstart, and `SIMULATE=1` for credential-free evaluation
- **The T2 disclosure** — the defect is seeded, not discovered in the wild. State it plainly; it costs one paragraph and buys credibility with an audience that builds agents for a living.
- The scale metrics you defined but deliberately did not build at n = 12: human-reject rate on verified PRs, cost per merged PR, self-report accuracy as a trend.
- Link to `DESIGN.md` for the argument.

---

## Repo layout

```
fixloop/
├── Dockerfile
├── docker-compose.yml
├── README.md
├── DESIGN.md
├── BUILD.md
├── bootstrap.py              # creates playbook / knowledge / secret, prints IDs
├── fixtures/                 # recorded API responses → SIMULATE=1
└── fixloop/
    ├── config.py             # env, limits, path allowlists, pinned scanner cmd
    ├── db.py                 # 2 tables, conditional-update helpers
    ├── scan.py               # pinned bandit → findings → fingerprints
    ├── github.py             # issues, labels, comments, PR state
    ├── devin.py              # create / list / message + status normalization
    ├── verify.py             # subprocess gates + class oracles
    ├── reconcile.py          # the tick
    ├── web.py                # status page
    └── prompts/
        ├── t1.md
        └── t2.md
```

## `.env`

```bash
DEVIN_API_KEY=cog_...
DEVIN_ORG_ID=org-...
DEVIN_PLAYBOOK_ID=
DEVIN_KNOWLEDGE_IDS=
DEVIN_SECRET_IDS=

GITHUB_TOKEN=
GITHUB_REPO=<you>/superset

TICK_SECONDS=30
SCAN_INTERVAL_HOURS=6
MAX_CONCURRENCY=3
DAILY_ACU_CEILING=100
MAX_ATTEMPTS=2
WALL_CLOCK_MINUTES=45
DRY_RUN=0
SIMULATE=0
```

---

## Delegating to Devin

Worth doing for the Loom as much as for the time. Hand it the mechanical, well-specified pieces while you write the verifier:

- Dockerfile and compose file
- Status page HTML and the Flask route
- `db.py` schema plus conditional-update helpers
- The fixtures-replay harness

**Keep yourself:** `verify.py` and the status normalization table. Both are small, and both are places where a subtle error silently invalidates the demo rather than failing loudly.

---

## Cut order, if the day runs long

T2 → simulate replay → advisory Devin Review → status page.

The issue comments are the primary artifact. Losing the status page costs polish, not the story.

---

## Deliverables checklist

- [ ] Public solution repo, Dockerized, README with run + simulate instructions
- [ ] Forked Superset with the issues filed, PRs opened, evidence comments visible
- [ ] Loom ≤ 5 min: what / how / why Devin / what's next
- [ ] At least one visible rejection → repair → pass cycle
