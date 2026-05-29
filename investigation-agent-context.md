# Custom Investigation Agent — Project Context for Claude Code

Paste this file into your Claude Code terminal session to continue building the custom investigation layer without losing any context.

---

## Background

We built an end-to-end Agentic SRE system on AWS using AWS DevOps Agent as the investigation layer. The system successfully:
- Detects incidents via CloudWatch alarms
- Routes events through EventBridge → Lambda → webhook
- Investigates root causes autonomously
- Correlates GitHub commits, CloudWatch metrics, Splunk logs, and CloudTrail events
- Produces detailed root cause analysis and mitigation plans

The lead has reviewed the system and requested that the **AWS DevOps Agent investigation layer be replaced with a custom-built agent** using LangGraph, giving us full ownership of investigation logic and portability.

---

## Lead's requirements (from meeting)

| Requirement | Decision |
|---|---|
| Replace AWS DevOps Agent | Yes — in production. Both run in parallel during testing only |
| Investigation logic ownership | Full — we own orchestration AND LLM reasoning/prompts |
| Agent actions | Report only — no rollbacks, no deployments |
| Deployment target | AWS Lambda (serverless) for now |
| LLM provider | Configurable via environment variable |
| LLM API calls | Direct API calls allowed (not through Bedrock) |
| Mitigation plan | Yes — generate same as AWS DevOps Agent |
| Investigation history | Yes — stored in DynamoDB, searchable |
| Human-in-the-loop (HITL) | Yes — Slack review thread, any one approval publishes |
| Benchmark | AWS DevOps Agent runs in parallel during testing for comparison |
| Goal | Mimic and eventually exceed AWS DevOps Agent quality |

---

## What the custom agent must do

Replicate everything AWS DevOps Agent did during investigations:

1. Receive incident webhook payload from CloudWatch alarm
2. Fetch alarm details from CloudWatch
3. Run parallel investigations:
   - Fetch CloudWatch metrics (ErrorCount, CPU, network)
   - Fetch application logs from Splunk / CloudWatch Logs
   - Fetch CloudTrail events (deployments, API calls, SSH sessions)
   - Fetch GitHub commits and PRs merged before the incident
4. LLM synthesizes all findings into root cause + mitigation plan
5. Store investigation in DynamoDB
6. Post to Slack HITL review thread for approval
7. On approval — publish to main #incidents Slack channel

---

## Full architecture

```
CloudWatch Alarm
    → EventBridge
        → Lambda (existing webhook forwarder — already built)
            → SQS Queue (buffer)
                → Investigation Lambda (LangGraph agent — NEW)
                    ├── Tool: fetch_alarm_details     (CloudWatch)
                    ├── Tool: fetch_metrics           (CloudWatch)
                    ├── Tool: fetch_logs              (Splunk + CloudWatch Logs)
                    ├── Tool: fetch_cloudtrail        (CloudTrail)
                    └── Tool: fetch_github_commits    (GitHub API)
                              ↓
                         LLM Synthesis (Claude / configurable)
                              ↓
                         Root cause + mitigation plan
                              ↓
                         Store in DynamoDB
                              ↓
                         Post to Slack HITL thread
                              ↓
                         Reviewer approves (any one ✅ reaction)
                              ↓
                         Publish to #incidents channel
```

---

## HITL Slack flow

```
Investigation completes
    ↓
Agent posts to #sre-review (private):
    "🔍 New investigation: flask-api-errors
     Root cause: [summary]
     Mitigation: [steps]
     Full report: [DynamoDB link]
     React ✅ to approve and publish to #incidents"
    ↓
Any reviewer reacts with ✅
    ↓
Slack Events API webhook fires reaction event to Lambda
    ↓
Lambda publishes full report to #incidents
    ↓
DynamoDB updated: status=published, approved_by=user
```

---

## Parallel testing strategy (during transition)

```
CloudWatch Alarm fires
    ↓
    ├── Existing Lambda → AWS DevOps Agent     (benchmark)
    └── New SQS trigger → LangGraph Agent      (our system)
              ↓
    Both post to separate Slack channels:
    - #devops-agent-findings  (benchmark — existing)
    - #sre-review             (HITL — new agent)
              ↓
    Team compares quality side by side
    When confident → disable AWS DevOps Agent
```

---

## Project structure to build

```
investigation-agent/
├── agent/
│   ├── graph.py                  # LangGraph graph definition
│   ├── state.py                  # InvestigationState TypedDict
│   └── nodes/
│       ├── fetch_alarm.py        # CloudWatch alarm details
│       ├── fetch_metrics.py      # CloudWatch metrics (ErrorCount, CPU)
│       ├── fetch_logs.py         # Splunk + CloudWatch Logs
│       ├── fetch_cloudtrail.py   # CloudTrail events
│       ├── fetch_github.py       # GitHub commits + PRs
│       └── synthesize.py         # LLM root cause + mitigation
├── storage/
│   └── dynamodb.py               # Investigation history store
├── notifications/
│   └── slack.py                  # HITL thread + approval handler
├── handler.py                    # Lambda entry point
├── config.py                     # LLM provider config
├── requirements.txt
└── infra/
    └── template.yaml             # CloudFormation: Lambda + DynamoDB + SQS
```

---

## InvestigationState schema

```python
from typing import TypedDict, List, Optional

class InvestigationState(TypedDict):
    # Input
    incident: dict                  # raw webhook payload from CloudWatch
    incident_id: str                # unique ID for DynamoDB

    # Investigation data (populated by parallel nodes)
    alarm_details: dict             # CloudWatch alarm config + history
    metrics: List[dict]             # ErrorCount, CPU, network datapoints
    logs: List[dict]                # Splunk / CloudWatch log entries
    cloudtrail_events: List[dict]   # API calls, deployments, SSH sessions
    github_commits: List[dict]      # recent commits + PRs

    # Synthesis output
    observations: List[str]         # intermediate findings from each node
    root_cause: str                 # final root cause statement
    mitigation_plan: str            # step-by-step mitigation
    investigation_gaps: List[str]   # what data was missing

    # Metadata
    status: str                     # investigating / pending_review / published
    approved_by: Optional[str]      # Slack user who approved
    slack_thread_ts: Optional[str]  # Slack thread timestamp for HITL
```

---

## LangGraph graph definition (skeleton)

```python
from langgraph.graph import StateGraph, END
from agent.state import InvestigationState
from agent.nodes import (
    fetch_alarm, fetch_metrics, fetch_logs,
    fetch_cloudtrail, fetch_github, synthesize
)

def build_graph():
    graph = StateGraph(InvestigationState)

    # Add nodes
    graph.add_node("fetch_alarm",      fetch_alarm.run)
    graph.add_node("fetch_metrics",    fetch_metrics.run)
    graph.add_node("fetch_logs",       fetch_logs.run)
    graph.add_node("fetch_cloudtrail", fetch_cloudtrail.run)
    graph.add_node("fetch_github",     fetch_github.run)
    graph.add_node("synthesize",       synthesize.run)

    # Entry point
    graph.set_entry_point("fetch_alarm")

    # After alarm fetch — fan out to parallel investigations
    graph.add_edge("fetch_alarm", "fetch_metrics")
    graph.add_edge("fetch_alarm", "fetch_logs")
    graph.add_edge("fetch_alarm", "fetch_cloudtrail")
    graph.add_edge("fetch_alarm", "fetch_github")

    # All parallel branches converge at synthesize
    graph.add_edge("fetch_metrics",    "synthesize")
    graph.add_edge("fetch_logs",       "synthesize")
    graph.add_edge("fetch_cloudtrail", "synthesize")
    graph.add_edge("fetch_github",     "synthesize")

    # End
    graph.add_edge("synthesize", END)

    return graph.compile()
```

---

## LLM configuration (configurable provider)

```python
# config.py
import os
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

def get_llm():
    provider = os.environ.get("LLM_PROVIDER", "anthropic")

    if provider == "anthropic":
        return ChatAnthropic(
            model=os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514"),
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            max_tokens=4096
        )
    elif provider == "openai":
        return ChatOpenAI(
            model=os.environ.get("LLM_MODEL", "gpt-4o"),
            api_key=os.environ.get("OPENAI_API_KEY")
        )
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")
```

---

## Synthesis prompt (starting point)

```python
SYNTHESIS_PROMPT = """
You are a senior SRE investigating a production incident.

## Incident
{incident_summary}

## CloudWatch Alarm Details
{alarm_details}

## Metrics (last 30 minutes)
{metrics}

## Application Logs
{logs}

## CloudTrail Events (last 2 hours)
{cloudtrail_events}

## Recent GitHub Commits and Deployments
{github_commits}

## Your task
Based on all evidence above:

1. IMPACT: Describe what broke and when (specific timestamps)
2. ROOT CAUSE: Identify the single most likely root cause with supporting evidence
3. CONTRIBUTING FACTORS: List any secondary causes or conditions
4. MITIGATION PLAN: Provide step-by-step remediation (Prepare → Pre-validate → Apply → Post-validate)
5. INVESTIGATION GAPS: List what data was missing that would have helped

Be specific — reference exact timestamps, commit hashes, log lines, and metric values.
If evidence is insufficient, say so clearly rather than guessing.
Format your response as structured JSON.
"""
```

---

## DynamoDB schema

Table name: `sre-investigations`

```
PK: investigation_id (UUID)
SK: timestamp (ISO)

Attributes:
- incident_id        (from webhook)
- alarm_name         (flask-api-errors etc)
- status             (investigating / pending_review / published / dismissed)
- root_cause         (string)
- mitigation_plan    (string)
- observations       (list)
- investigation_gaps (list)
- raw_data           (metrics, logs, cloudtrail, github — full JSON)
- slack_thread_ts    (for HITL thread reference)
- approved_by        (Slack user ID)
- created_at         (ISO timestamp)
- published_at       (ISO timestamp)
- llm_provider       (anthropic / openai)
- llm_model          (model name used)
```

---

## Slack notification format

### HITL review message (posted to #sre-review)
```
🔍 *New Investigation — flask-api-errors*
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

*Impact*
CloudWatch alarm triggered at 11:39 UTC.
FlaskApp/ErrorCount reached 15 (threshold: 10).

*Root Cause*
Commit 9475560 deployed via CodeDeploy at 11:36 UTC
introduced a call to inventory-service.internal with
an impossibly short timeout (0.001s). DNS resolution
fails on every request causing 100% error rate on
GET /items.

*Mitigation Plan*
1. Prepare: identify the bad commit
2. Pre-validate: confirm inventory-service.internal
   does not exist in DNS
3. Apply: revert commit 9475560 and redeploy
4. Post-validate: confirm ErrorCount returns to 0

*Investigation gaps*
- No shell history available from EC2 instance

React ✅ to approve and publish to #incidents
React ❌ to dismiss this investigation
```

### Published message (posted to #incidents after approval)
```
✅ *Incident Resolved — flask-api-errors*
Approved by @shankar

[full report as above]

Investigation ID: inv-uuid-here
View full details: [DynamoDB console link]
```

---

## Existing infrastructure to reuse (already deployed)

| Resource | Details |
|---|---|
| EC2 instance | i-0dc405dc4a6d3c1eb, eu-north-1 |
| Flask app | Running on port 5000 with Splunk + CloudWatch logging |
| CloudWatch alarms | flask-api-errors, flask-api-cpu-high, flask-api-health-check |
| EventBridge rule | cloudwatch-alarm-to-devops-agent (eu-north-1) |
| Lambda webhook forwarder | devops-agent-webhook-forwarder (eu-north-1) |
| GitHub repo | shankarmutkekar-sonnet/agentic-sre |
| Splunk Cloud | https://prd-p-uaboh.splunkcloud.com:8088 |
| Slack workspace | Factspan Inc |
| AWS account | 682975283486 |
| CodePipeline | agentic-sre-pipeline (eu-north-1) |
| CodeDeploy | agentic-sre-app / agentic-sre-dg |

---

## AWS environment variables needed for Investigation Lambda

```bash
# LLM
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=<your key>

# AWS
AWS_REGION=eu-north-1
CLOUDWATCH_LOG_GROUP=/flask-app/logs

# GitHub
GITHUB_TOKEN=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
GITHUB_REPO=shankarmutkekar-sonnet/agentic-sre

# Splunk
SPLUNK_URL=https://prd-p-uaboh.splunkcloud.com
SPLUNK_TOKEN=d8cf307b-b62a-47aa-9c4b-687159bec523
SPLUNK_INDEX=main

# Slack
SLACK_BOT_TOKEN=<your bot token>
SLACK_REVIEW_CHANNEL=<#sre-review channel ID>
SLACK_INCIDENTS_CHANNEL=<#incidents channel ID>

# DynamoDB
DYNAMODB_TABLE=sre-investigations
```

---

## Build order (suggested)

### Phase 1 — Core agent
1. `agent/state.py` — InvestigationState TypedDict
2. `agent/nodes/fetch_alarm.py` — CloudWatch alarm details
3. `agent/nodes/fetch_metrics.py` — CloudWatch metrics
4. `agent/nodes/fetch_logs.py` — CloudWatch Logs + Splunk
5. `agent/nodes/fetch_cloudtrail.py` — CloudTrail events
6. `agent/nodes/fetch_github.py` — GitHub commits + PRs
7. `agent/nodes/synthesize.py` — LLM synthesis node
8. `agent/graph.py` — LangGraph graph wiring
9. `config.py` — LLM provider config
10. `handler.py` — Lambda entry point
11. Test locally against Scenario 1 incident data
12. Compare output with AWS DevOps Agent findings

### Phase 2 — Storage and HITL
1. `storage/dynamodb.py` — save + retrieve investigations
2. `notifications/slack.py` — HITL thread posting
3. Slack Events API webhook for reaction handling
4. Approval flow — reaction → publish to #incidents
5. `infra/template.yaml` — Lambda + DynamoDB + SQS CloudFormation

### Phase 3 — Polish
1. LLM provider switching tests
2. Error handling and retries for all tool calls
3. Investigation quality improvements
4. Side-by-side comparison report vs AWS DevOps Agent
5. Documentation

---

## Reference — AWS DevOps Agent investigation output to benchmark against

From Scenario 1 (inventory-service timeout bug), the Agent produced:

**Root cause:**
Commit 9475560 deployed via PR #11 replaced the working /items endpoint with a call to inventory-service.internal with a 0.001s timeout. DNS resolution fails (socket.gaierror: [Errno -2]) on every request causing 100% error rate on GET /items.

**Key findings:**
- Sudden error spike: FlaskApp/ErrorCount jumped from 0 to 15 in a single minute
- DNS resolution failure: 15 ERROR log entries between 11:39:12-11:39:15 UTC
- Deployment correlation: CodeDeploy deployment d-OQXUNOVLH at 11:36 UTC (3 min before errors)
- User activity: IAM user Shankar monitored alarms every ~1 min after deployment
- No infrastructure changes during incident window

**Investigation gaps identified by AWS DevOps Agent:**
- Cannot access instance shell history
- No code repository connected (before GitHub was added)

Your custom agent should produce output of equal or better quality than this.
