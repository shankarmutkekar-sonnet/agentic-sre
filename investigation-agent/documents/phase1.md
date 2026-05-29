# Phase 1 — Core Investigation Agent

**Status:** ✅ Complete  
**Goal:** Build the core LangGraph-based investigation pipeline that receives a CloudWatch alarm, gathers evidence from all data sources in parallel, and produces a structured root cause analysis using an LLM.

---

## What Phase 1 Delivers

| Capability | Delivered |
|---|---|
| Receive CloudWatch alarm via SQS | ✅ |
| Fetch alarm config + state history | ✅ |
| Fetch CloudWatch metrics (ErrorCount, CPU, Network) | ✅ |
| Fetch application logs (CloudWatch Logs + Splunk) | ✅ |
| Fetch CloudTrail events (deployments, SSH, IAM) | ✅ |
| Fetch GitHub commits + merged PRs | ✅ |
| LLM synthesis → root cause + mitigation plan | ✅ |
| Configurable LLM provider (Gemini / Anthropic / OpenAI) | ✅ |
| Parallel investigation (asyncio fan-out) | ✅ |
| Lambda entry point with SQS + direct invocation | ✅ |

**Not in Phase 1** (deferred to Phase 2):
- DynamoDB persistence
- Slack HITL review thread
- CloudFormation infrastructure

---

## Project Structure

```
investigation-agent/
├── agent/
│   ├── __init__.py
│   ├── state.py                  # Shared state TypedDict — the "notepad"
│   ├── graph.py                  # LangGraph orchestration — who runs when
│   └── nodes/
│       ├── __init__.py
│       ├── fetch_alarm.py        # Node 1: CloudWatch alarm details
│       ├── fetch_metrics.py      # Node 2: CloudWatch metric datapoints
│       ├── fetch_logs.py         # Node 3: CloudWatch Logs + Splunk
│       ├── fetch_cloudtrail.py   # Node 4: CloudTrail events
│       ├── fetch_github.py       # Node 5: GitHub commits + PRs
│       └── synthesize.py         # Node 6: LLM root cause analysis
├── config.py                     # LLM provider selector
├── handler.py                    # Lambda entry point
├── requirements.txt
├── .env                          # Secrets (never commit)
├── .env.example                  # Template (safe to commit)
├── .gitignore
└── documents/
    └── phase1.md                 # This file
```

---

## Architecture

### Execution Flow

```
CloudWatch Alarm fires
        │
        ▼
EventBridge Rule
        │
        ▼
Existing webhook Lambda          (lambda/index.mjs — already deployed)
        │  HMAC-signed POST
        ▼
SQS Queue                        (buffer — Phase 2 infrastructure)
        │
        ▼
Investigation Lambda             (handler.py — NEW)
        │
        ▼
┌───────────────────────────────────────────────────────┐
│                  LangGraph Graph                       │
│                                                        │
│              ┌─────────────┐                           │
│              │ fetch_alarm │  ← entry node (sequential)│
│              └──────┬──────┘                           │
│      ┌──────────────┼──────────────┬────────────────┐  │
│      ▼              ▼              ▼                ▼  │
│ fetch_metrics  fetch_logs  fetch_cloudtrail  fetch_github │
│   (parallel)   (parallel)    (parallel)      (parallel) │
│      └──────────────┼──────────────┴────────────────┘  │
│                     ▼                                   │
│                synthesize    ← LLM node (sequential)    │
└───────────────────────────────────────────────────────┘
        │
        ▼
Final InvestigationState
  root_cause, mitigation_plan, observations, gaps
        │
        ├──▶ _save_to_dynamodb()   (STUB — Phase 2)
        └──▶ _post_to_slack()      (STUB — Phase 2)
```

### Why Parallel?

The 4 investigation nodes (metrics, logs, cloudtrail, github) are independent —
they don't need each other's results. Running them in parallel with `asyncio`
means instead of:

```
fetch_metrics    5s
fetch_logs       6s   = 22s total (sequential)
fetch_cloudtrail 4s
fetch_github     7s
```

We get:

```
fetch_metrics  ─────┐
fetch_logs     ──────┤  = ~7s total (parallel)
fetch_cloudtrail────┤
fetch_github   ─────┘
```

Critical for staying within Lambda's 15-minute timeout when each AWS/HTTP
call takes several seconds.

---

## Module Breakdown

### `agent/state.py` — Shared State

The `InvestigationState` TypedDict acts as a shared notepad passed through
every node. Each node reads the current state and returns only the fields
it updates — LangGraph merges the partial updates automatically.

```
┌─────────────────────────────────────────────────────┐
│                 InvestigationState                   │
├──────────────┬──────────────────────────────────────┤
│ INPUT        │ incident, incident_id                 │
├──────────────┼──────────────────────────────────────┤
│ INVESTIGATION│ alarm_details   ← fetch_alarm writes  │
│ DATA         │ metrics         ← fetch_metrics writes │
│              │ logs            ← fetch_logs writes    │
│              │ cloudtrail_events ← fetch_cloudtrail  │
│              │ github_commits  ← fetch_github writes  │
├──────────────┼──────────────────────────────────────┤
│ SYNTHESIS    │ root_cause      ← synthesize writes   │
│              │ mitigation_plan ← synthesize writes   │
│              │ observations    ← ALL nodes append    │
│              │ investigation_gaps ← ALL nodes append │
├──────────────┼──────────────────────────────────────┤
│ METADATA     │ status, approved_by, slack_thread_ts  │
└──────────────┴──────────────────────────────────────┘
```

**Important:** `observations` and `investigation_gaps` use `operator.add`
reducers. When 4 parallel nodes all write `observations: ["my finding"]`,
LangGraph concatenates the lists into one instead of overwriting.

---

### `agent/graph.py` — Orchestrator

Defines the execution order using LangGraph's `StateGraph`.

```python
graph.set_entry_point("fetch_alarm")        # runs first

graph.add_edge("fetch_alarm", "fetch_metrics")    # ─┐
graph.add_edge("fetch_alarm", "fetch_logs")       #  ├─ fan-out (parallel)
graph.add_edge("fetch_alarm", "fetch_cloudtrail") #  │
graph.add_edge("fetch_alarm", "fetch_github")     # ─┘

graph.add_edge("fetch_metrics",    "synthesize")  # ─┐
graph.add_edge("fetch_logs",       "synthesize")  #  ├─ fan-in (converge)
graph.add_edge("fetch_cloudtrail", "synthesize")  #  │
graph.add_edge("fetch_github",     "synthesize")  # ─┘

graph.add_edge("synthesize", END)
```

The compiled graph is stored as `investigation_graph` at module level — this
means it is compiled once and reused across Lambda warm invocations.

---

### Node 1: `fetch_alarm.py`

**Runs:** First, sequentially  
**Data source:** AWS CloudWatch  
**boto3 calls:**
- `describe_alarms(AlarmNames=[alarm_name])` → threshold, namespace, dimensions, state
- `describe_alarm_history(...)` → last 10 state transitions in 2-hour window

**How it extracts the alarm name** (3 fallbacks):
1. `incident["data"]["alarmName"]` — webhook forwarder shape
2. `incident["detail"]["alarmName"]` — raw EventBridge shape
3. Split `incident["title"]` on `:` — last resort

**Writes to state:** `alarm_details`, `observations[0]`, `status = "investigating"`

---

### Node 2: `fetch_metrics.py`

**Runs:** Parallel (after fetch_alarm)  
**Data source:** AWS CloudWatch  
**boto3 calls:**
- Single `get_metric_data()` call with up to 4 metric queries:

| Metric | Namespace | Stat | Period |
|---|---|---|---|
| ErrorCount | FlaskApp | Sum | 60s |
| CPUUtilization | AWS/EC2 | Average | 60s |
| NetworkIn | AWS/EC2 | Average | 60s |
| NetworkOut | AWS/EC2 | Average | 60s |

**Window:** 30 minutes before incident + 5 minutes after (so spike is never at edge)

**EC2 instance ID resolution:**
1. Alarm dimensions from `alarm_details.config.dimensions` (set by fetch_alarm)
2. `EC2_INSTANCE_ID` environment variable
3. Skip EC2 metrics gracefully, add gap

**Writes to state:** `metrics` (list of datapoints), `observations`, `investigation_gaps`

---

### Node 3: `fetch_logs.py`

**Runs:** Parallel  
**Data sources:** CloudWatch Logs + Splunk Cloud  
**Queries both concurrently** using `asyncio.gather()`

**CloudWatch Logs:**
- `filter_log_events()` on log group `CLOUDWATCH_LOG_GROUP` (`/flask-app/logs`)
- 30-minute window, max 200 events

**Splunk:**
- REST API: `POST /services/search/jobs/export`
- SPL: `search index=main source="ec2-flask-app" earliest=... latest=... | head 200`
- Uses `exec_mode=oneshot` (blocks until complete — suitable for Lambda)
- Uses stdlib `urllib` (no extra dependency needed)
- SSL verification disabled for Splunk Cloud self-signed cert

**Normalised output shape:**
```python
{
  "source":    "cloudwatch_logs" | "splunk",
  "timestamp": "2026-05-25T11:39:12Z",
  "level":     "ERROR",
  "message":   "socket.gaierror: [Errno -2] Name or service not known"
}
```

**Writes to state:** `logs`, `observations`, `investigation_gaps`

---

### Node 4: `fetch_cloudtrail.py`

**Runs:** Parallel  
**Data source:** AWS CloudTrail  
**boto3 calls:**
- `lookup_events()` paginated, 2-hour window, max 100 events

**Filters to relevant event sources only:**
```
codedeploy.amazonaws.com  → deployments
ec2.amazonaws.com         → instance changes
iam.amazonaws.com         → privilege escalation, key changes
cloudwatch.amazonaws.com  → alarm/threshold changes
ssm.amazonaws.com         → SSM sessions (proxy for SSH)
s3.amazonaws.com          → CodeDeploy artifact uploads
```

**High-signal events** (always surfaced in observation):
- `CreateDeployment`, `StartSession`, `TerminateInstances`, `AssumeRole`,
  `CreateAccessKey`, `AttachUserPolicy`

**Writes to state:** `cloudtrail_events`, `observations`, `investigation_gaps`

---

### Node 5: `fetch_github.py`

**Runs:** Parallel  
**Data source:** GitHub REST API v3  
**Uses:** stdlib `urllib` only (no PyGitHub dependency)

**Fetches:**
- Commits to `main` branch in the **60 minutes** before the incident
  - Includes diff stats per commit (files changed, additions, deletions)
- Merged PRs in the **2 hours** before the incident

**Authentication:** `Authorization: Bearer {GITHUB_TOKEN}`

**Output shape:**
```python
# Commit
{"type": "commit", "sha": "9475560", "message": "...", "author": "Shankar",
 "authored_at": "...", "additions": 45, "deletions": 12, "files_changed": 3}

# Pull request
{"type": "pull_request", "number": 11, "title": "...", "merged_at": "..."}
```

**Writes to state:** `github_commits`, `observations`, `investigation_gaps`

---

### Node 6: `synthesize.py`

**Runs:** Last, after all 4 parallel nodes complete  
**Data source:** LLM (Gemini 2.0 Flash by default)

**Builds a prompt with all 5 data sections:**
```
Incident summary
CloudWatch Alarm Details
Metrics (last 30 min)
Application Logs
CloudTrail Events (last 2 hrs)
GitHub Commits and PRs
All intermediate observations
```

**Each section is truncated** at 6,000 characters (logs: 8,000) to stay
within LLM context limits.

**Instructs the LLM to respond in strict JSON:**
```json
{
  "impact": "...",
  "root_cause": "...",
  "contributing_factors": ["..."],
  "mitigation_plan": "...",
  "investigation_gaps": ["..."],
  "confidence": "HIGH | MEDIUM | LOW",
  "evidence_summary": "..."
}
```

**JSON parse strategy** (3 fallbacks):
1. Direct `json.loads(response)`
2. Strip markdown fences, retry parse
3. Find first `{...}` block, retry parse
4. Store raw text under `root_cause` with `confidence: LOW`

**Writes to state:** `root_cause`, `mitigation_plan`, `investigation_gaps`,
`observations`, `status = "pending_review"`

---

### `config.py` — LLM Provider

```
LLM_PROVIDER=gemini      → ChatGoogleGenerativeAI(gemini-2.0-flash)
LLM_PROVIDER=anthropic   → ChatAnthropic(claude-sonnet-4-6)
LLM_PROVIDER=openai      → ChatOpenAI(gpt-4o)
```

Switch providers by changing `LLM_PROVIDER` in `.env` — no code changes needed.

---

### `handler.py` — Lambda Entry Point

**Two invocation modes:**

```
Mode 1: SQS trigger (production)
  event["Records"] = list of SQS messages
  → each record.body is parsed as incident JSON

Mode 2: Direct invocation (local testing)
  event = raw incident JSON (no Records wrapper)
  → handler wraps it as a synthetic SQS record
```

**Per-record flow:**
```python
incident = parse_sqs_record(record)
incident_id = f"inv-{uuid4()}"
initial_state = build_initial_state(incident, incident_id)
final_state = await investigation_graph.ainvoke(initial_state)
_save_to_dynamodb(final_state)   # STUB — Phase 2
_post_to_slack(final_state)      # STUB — Phase 2
```

Records are processed **sequentially** (not concurrently) to prevent
multiple simultaneous LLM calls from blowing the Lambda timeout on alarm bursts.

---

## End-to-End Example — Scenario 1 (Inventory Service Bug)

**Incident:** Commit `9475560` introduced a call to `inventory-service.internal`
with a 0.001s timeout. DNS fails → 100% error rate on `GET /items`.

### Timeline

```
11:34Z  Commit 9475560 pushed to GitHub (PR #11)
11:35Z  PR #11 merged to main
11:36Z  CodeDeploy deployment started (CloudTrail: CreateDeployment by Shankar)
11:37Z  App restarted with bad code
11:39Z  First errors hit CloudWatch — ErrorCount = 15 in one minute
11:39Z  flask-api-errors alarm fires → ALARM state
11:39Z  EventBridge → webhook Lambda → SQS → Investigation Lambda wakes up
```

### What each node finds

| Node | Key finding |
|---|---|
| `fetch_alarm` | Alarm in ALARM, threshold=10, reason="15 datapoints > 10", 2 transitions in 2hrs |
| `fetch_metrics` | ErrorCount: peak=15, only 1 non-zero minute out of 30; CPU: normal at 12% |
| `fetch_logs` | 15 ERROR entries at 11:39:12–11:39:15Z: `socket.gaierror: [Errno -2] inventory-service.internal` |
| `fetch_cloudtrail` | `CreateDeployment` by Shankar at 11:36Z (3 min before errors) |
| `fetch_github` | Commit `9475560` "refactor: integrate inventory-service" (+45/-12, 3 files), PR #11 merged at 11:35Z |

### Gemini output

```json
{
  "impact": "GET /items endpoint failed with 100% error rate from 11:39:12Z.
             FlaskApp/ErrorCount hit 15 in a single minute (threshold: 10).",

  "root_cause": "Commit 9475560 deployed via PR #11 at 11:36Z replaced the
                 working /items endpoint with a call to inventory-service.internal
                 with a 0.001s timeout. DNS resolution fails on every request
                 (socket.gaierror: [Errno -2]) causing 100% error rate on GET /items.",

  "contributing_factors": [
    "inventory-service.internal does not exist in DNS",
    "0.001s timeout is too short even if the service existed"
  ],

  "mitigation_plan": "1. Prepare: identify commit 9475560 as root cause\n
                      2. Pre-validate: confirm DNS lookup for inventory-service.internal fails\n
                      3. Apply: revert commit 9475560 via CodeDeploy rollback\n
                      4. Post-validate: confirm ErrorCount returns to 0 within 1 minute",

  "investigation_gaps": ["No shell history from EC2 instance available"],

  "confidence": "HIGH",

  "evidence_summary": "DNS failure at 11:39:12Z matches CodeDeploy deployment at
                       11:36Z (3-min gap). Commit 9475560 explicitly references
                       inventory-service integration. CPU is normal (12%), ruling
                       out resource exhaustion as cause."
}
```

---

## Environment Variables

| Variable | Required | Value |
|---|---|---|
| `LLM_PROVIDER` | Yes | `gemini` |
| `LLM_MODEL` | Yes | `gemini-2.0-flash` |
| `GOOGLE_API_KEY` | Yes | Gemini API key from AI Studio |
| `AWS_REGION` | Yes | `eu-north-1` |
| `EC2_INSTANCE_ID` | Yes | `i-0dc405dc4a6d3c1eb` |
| `CLOUDWATCH_LOG_GROUP` | Yes | `/flask-app/logs` |
| `GITHUB_TOKEN` | Yes | GitHub PAT |
| `GITHUB_REPO` | Yes | `shankarmutkekar-sonnet/agentic-sre` |
| `SPLUNK_URL` | Yes | `https://prd-p-uaboh.splunkcloud.com` |
| `SPLUNK_TOKEN` | Yes | Splunk HEC token |
| `SPLUNK_INDEX` | Yes | `main` |
| `SLACK_BOT_TOKEN` | Phase 2 | `xoxb-...` |
| `SLACK_REVIEW_CHANNEL` | Phase 2 | `C0B701B89BN` |
| `SLACK_INCIDENTS_CHANNEL` | Phase 2 | `C0B5XCQLP9C` |
| `SLACK_SIGNING_SECRET` | Phase 2 | From Slack App settings |
| `DYNAMODB_TABLE` | Phase 2 | `sre-investigations` |

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `langgraph` | `>=1.2.2` | Graph orchestration + parallel execution |
| `langchain-core` | `>=1.4.0` | LangChain base (HumanMessage, etc.) |
| `langchain-google-genai` | `>=4.2.4` | Gemini provider |
| `langchain-anthropic` | `>=1.4.4` | Anthropic provider (standby) |
| `langchain-openai` | `>=1.2.2` | OpenAI provider (standby) |
| `boto3` | `>=1.38.0` | AWS SDK (CloudWatch, CloudTrail, Logs) |
| `python-dotenv` | `>=1.1.0` | Load `.env` locally (skipped in Lambda) |

All HTTP to Splunk and GitHub uses Python's stdlib `urllib` — no `requests`
dependency needed.

---

## Local Test Command

Once `GOOGLE_API_KEY` is set in `.env`, run a local test by invoking the
handler directly with a synthetic incident payload:

```bash
cd investigation-agent

python - <<'EOF'
import asyncio, json
from handler import handler

# Simulate the exact payload from the Scenario 1 incident
test_event = {
  "title": "CloudWatch Alarm: flask-api-errors",
  "service": "ec2-flask-api",
  "action": "created",
  "priority": "HIGH",
  "timestamp": "2026-05-25T11:39:00Z",
  "description": "Threshold crossed: 15 datapoints > 10 threshold",
  "data": {
    "alarmName": "flask-api-errors",
    "alarmArn": "arn:aws:cloudwatch:eu-north-1:682975283486:alarm:flask-api-errors",
    "accountId": "682975283486",
    "region": "eu-north-1",
    "currentState": "ALARM",
    "previousState": "OK",
    "stateReason": "Threshold crossed: 15 datapoints > 10 threshold"
  }
}

result = handler(test_event, None)
print(json.dumps(json.loads(result["body"]), indent=2))
EOF
```

---

## What Phase 2 Adds

| Feature | Module |
|---|---|
| Save investigation to DynamoDB | `storage/dynamodb.py` |
| Post HITL review to `#agentic-sre-review` | `notifications/slack.py` |
| Handle ✅ reaction → publish to `#agentic-sre-incident` | `notifications/slack.py` |
| CloudFormation: Lambda + DynamoDB + SQS | `infra/template.yaml` |
