# Agentic SRE — Complete Technical Reference

> **Audience**: New developers joining the project.
> **Purpose**: Everything you need to understand, reproduce, and extend this system from scratch.
> **Last updated**: 2026-06-02

---

## Table of Contents

1. [What Is This Project?](#1-what-is-this-project)
2. [Origin Story — From AWS DevOps Agent to Custom Agent](#2-origin-story)
3. [System Architecture](#3-system-architecture)
4. [AWS Infrastructure](#4-aws-infrastructure)
5. [The Flask Application (Incident Generator)](#5-the-flask-application)
6. [The Investigation Agent — Component by Component](#6-the-investigation-agent)
7. [The Slack HITL Flow](#7-the-slack-hitl-flow)
8. [Building From Scratch — Step-by-Step](#8-building-from-scratch)
9. [Environment Variables Reference](#9-environment-variables-reference)
10. [Deployment Procedures](#10-deployment-procedures)
11. [Bug Scenarios](#11-bug-scenarios)
12. [Comparison: Custom Agent vs AWS DevOps Agent](#12-comparison)
13. [Known Issues & Lessons Learned](#13-known-issues--lessons-learned)
14. [Troubleshooting Runbook](#14-troubleshooting-runbook)
15. [Future Roadmap](#15-future-roadmap)

---

## 1. What Is This Project?

This project builds a **production-grade Agentic SRE (Site Reliability Engineering) system** — an AI-powered autonomous incident investigation pipeline.

### The core idea

When something breaks in production (e.g., a high error rate on a Flask API), instead of a human SRE being paged and manually digging through logs, metrics, CloudTrail, and GitHub history — **an AI agent does it automatically** and presents a structured root cause analysis with a mitigation plan for human review.

### Analogy

Think of it like a **medical diagnostic system**:

- The **CloudWatch Alarm** is the patient's vital signs monitor going off.
- The **Investigation Agent** is the on-call doctor who runs blood tests (metrics), checks X-rays (logs), reviews the patient's history (CloudTrail + GitHub), and produces a diagnosis (root cause) with a treatment plan (mitigation).
- The **Slack HITL review** is the senior consultant who signs off before the treatment is administered.

### What it does

```
Production incident occurs
        ↓
CloudWatch alarm fires automatically
        ↓
AI agent investigates in parallel:
  ├── What exactly alarmed? (CloudWatch alarm details)
  ├── What do the logs say? (CloudWatch Logs + Splunk)
  ├── What do the metrics show? (ErrorCount, CPU, network)
  ├── Was there a recent deployment? (GitHub commits + PRs)
  └── Were there any infra changes? (CloudTrail events)
        ↓
Claude (LLM) synthesises all findings into:
  - Root cause statement
  - Mitigation plan
        ↓
Slack HITL card posted for human review
        ↓
Human approves → full incident report published
```

---

## 2. Origin Story

### Phase 1 — AWS DevOps Agent (the benchmark)

Before building anything custom, I integrated **AWS DevOps Agent** — a recently launched AWS managed service (console at `aidevops.global.app.aws`) that performs agentic SRE investigations.

An **Agent Space** named `agentic-sre-agent` was configured in `eu-west-1` (Ireland) with:
- GitHub integration (reads commits and PRs)
- CloudWatch integration (reads metrics and alarms)
- Slack integration (posts findings)
- A webhook Lambda (`devops-agent-webhook-forwarder`) that forwards CloudWatch alarm events to the Agent Space

This gave the team a **working baseline** and a quality benchmark to compare against.

### Phase 2 — Custom LangGraph Agent (what we built)

The lead (Arush Agarwal) requested that the AWS DevOps Agent be **replaced with a fully custom-built agent** using:
- **LangGraph** — for orchestrating the investigation as a stateful graph
- **Claude (Anthropic)** — as the LLM for synthesis
- **AWS Lambda** — as the serverless compute layer
- **DynamoDB** — for investigation history
- **Slack Block Kit** — for the HITL review interface

**Why build custom?**
- Full ownership of investigation logic and prompts
- Portable — not tied to one AWS service
- Configurable LLM provider (switch between Anthropic, OpenAI, Google)
- Extensible — add new investigation nodes without AWS service limits
- Cost control — pay only for what you use

### Phase 3 — Parallel comparison (current state)

Both systems run **simultaneously on the same alarms**:

| | AWS DevOps Agent | Custom Agent |
|---|---|---|
| Trigger | EventBridge → webhook Lambda → Agent Space | EventBridge → SQS → Investigation Lambda |
| Region | eu-west-1 | eu-north-1 |
| LLM | AWS managed | Claude Haiku (configurable) |
| Output | Agent Space console + Slack | Slack HITL card + DynamoDB |

The goal: match then exceed AWS DevOps Agent quality, then disable it.

---

## 3. System Architecture

### High-level data flow

```
EC2 Flask App (eu-north-1)
  │
  ├── /var/log/flask-app/app.log ──────────────────────────────────────────────┐
  ├── CloudWatch Agent → /flask-app/logs log group ──────────────────────────┐ │
  ├── Splunk HEC → prd-p-uaboh.splunkcloud.com (index: main) ─────────────┐  │ │
  └── boto3 → CloudWatch Metrics (FlaskApp/ErrorCount) ─────────────────┐ │  │ │
                                                                        │ │  │ │
CloudWatch Alarm: flask-api-errors                                      │ │  │ │
  (ErrorCount > 10 over 1 × 60s)                                        │ │  │ │
        │                                                               │ │  │ │
        ↓ state change → ALARM                                          │ │  │ │
                                                                        │ │  │ │
EventBridge Rule: sre-cloudwatch-alarm-to-sqs                           │ │  │ │
  [Input Transformer → incident JSON]                                   │ │  │ │
        │                                                               │ │  │ │
        ↓                                                               │ │  │ │
SQS Queue: sre-investigation-queue                                      │ │  │ │
  (VisibilityTimeout: 960s, DLQ after 2 attempts)                       │ │  │ │
        │                                                               │ │  │ │
        ↓ triggers                                                      │ │  │ │
                                                                        │ │  │ │
Lambda: sre-investigation-agent                                         │ │  │ │
  (Python 3.12, 512MB, 900s timeout, VPC private subnet)                │ │  │ │
        │                                                               │ │  │ │
        ├── fetch_alarm    ←── CloudWatch DescribeAlarms ───────────────┘ │  │ │
        ├── fetch_logs     ←── CloudWatch FilterLogEvents ────────────────┘  │ │
        │                  ←── Splunk REST API (port 8089 via NAT GW) ───────┘ │
        ├── fetch_metrics  ←── CloudWatch GetMetricData ───────────────────────┘
        ├── fetch_cloudtrail ← CloudTrail LookupEvents
        └── fetch_github   ←── GitHub REST API v3
                │
                ↓ (all run in parallel via asyncio.gather)
                │
        synthesise (Claude Haiku via Anthropic API)
                │
                ├── DynamoDB: sre-investigations (status: pending_review)
                │
                └── Slack: #agentic-sre-review
                      [Block Kit card with Approve / Dismiss buttons]
                              │
                              ↓ button click
                      API Gateway: sre-slack-events-api
                              │
                              ↓
                      Lambda: sre-slack-webhook
                              │
                              ├── Approve → #agentic-sre-incident (full report)
                              │           → DynamoDB status = "approved"
                              └── Dismiss → DynamoDB status = "dismissed"
```

### VPC networking (added for Splunk connectivity)

```
SRE VPC (10.0.0.0/16)
  │
  ├── Public Subnet (10.0.1.0/24)
  │     └── NAT Gateway ←── Elastic IP: 13.63.151.169 (whitelisted in Splunk Cloud)
  │                ↑
  │           Internet Gateway
  │
  └── Private Subnet (10.0.2.0/24)
        └── Lambda ENI (sre-investigation-agent)
              └── all outbound traffic → NAT Gateway → internet
```

**Why VPC?** Splunk Cloud firewalls port 8089 to specific IP addresses only. Lambda normally has a dynamic IP that changes on every cold start. Placing Lambda in a VPC with a NAT Gateway gives it a fixed outbound Elastic IP that can be whitelisted in Splunk Cloud.

---

## 4. AWS Infrastructure

### Region
All custom agent resources: **eu-north-1 (Stockholm)**
AWS DevOps Agent space: eu-west-1 (Ireland) — separate, not managed by this repo

### CloudFormation Stack
Stack name: **`agentic-sre-investigation`**
Template: `investigation-agent/infra/template.yaml`

| Resource | Type | Name / Value |
|---|---|---|
| VPC | AWS::EC2::VPC | sre-vpc (10.0.0.0/16) |
| Internet Gateway | AWS::EC2::InternetGateway | sre-igw |
| Public Subnet | AWS::EC2::Subnet | sre-public-subnet (10.0.1.0/24) |
| Private Subnet | AWS::EC2::Subnet | sre-private-subnet (10.0.2.0/24) |
| Elastic IP | AWS::EC2::EIP | 13.63.151.169 |
| NAT Gateway | AWS::EC2::NatGateway | sre-nat-gateway |
| Lambda Security Group | AWS::EC2::SecurityGroup | sre-lambda-sg (egress: 443, 8089) |
| Investigation Lambda | AWS::Lambda::Function | sre-investigation-agent |
| Slack Webhook Lambda | AWS::Lambda::Function | sre-slack-webhook |
| DynamoDB Table | AWS::DynamoDB::Table | sre-investigations |
| SQS Queue | AWS::SQS::Queue | sre-investigation-queue |
| SQS DLQ | AWS::SQS::Queue | sre-investigation-dlq |
| API Gateway | AWS::ApiGateway::RestApi | sre-slack-events-api |
| IAM Role (agent) | AWS::IAM::Role | sre-investigation-lambda-role |
| IAM Role (slack) | AWS::IAM::Role | sre-slack-webhook-lambda-role |

### EC2 Instance

| Property | Value |
|---|---|
| Instance ID | i-0dc405dc4a6d3c1eb |
| Public IP | 51.20.6.149 (dynamic — check if instance restarts) |
| OS | Amazon Linux 2 |
| App | Flask API on port 5000 (Gunicorn, 2 workers) |
| IAM Role | agentic-sre-ec2-role |
| Deployment | CodeDeploy + CodePipeline from GitHub master |
| Log path | /var/log/flask-app/app.log |
| CloudWatch agent | Streams to /flask-app/logs |
| PID file | /home/ec2-user/flask-app.pid |

### EventBridge Rule

Rule name: **`sre-cloudwatch-alarm-to-sqs`**

Event pattern (catches ALL CloudWatch alarms going to ALARM state):
```json
{
  "source": ["aws.cloudwatch"],
  "detail-type": ["CloudWatch Alarm State Change"],
  "detail": {
    "state": {
      "value": ["ALARM"]
    }
  }
}
```

Input transformer (formats the EventBridge event into the incident payload the Lambda expects):
```json
{
  "title":     "CloudWatch Alarm: <alarmName>",
  "service":   "ec2-flask-api",
  "action":    "created",
  "priority":  "HIGH",
  "timestamp": "<time>",
  "data": {
    "alarmName": "<alarmName>",
    "state":     "<state>",
    "reason":    "<reason>",
    "region":    "<region>",
    "account":   "<account>"
  }
}
```

### CloudWatch Alarms

| Alarm | Metric | Threshold | Period |
|---|---|---|---|
| flask-api-errors | FlaskApp/ErrorCount | > 10 | 1 × 60s |
| SRE-ErrorRate | FlaskApp/ErrorCount | > threshold | configured |

### CodePipeline / CodeDeploy

- Pipeline name: `agentic-sre-pipeline`
- Source: GitHub `shankarmutkekar-sonnet/agentic-sre` → `master` branch
- Deploy target: EC2 `i-0dc405dc4a6d3c1eb`
- CodeDeploy app: `agentic-sre-app`
- Deployment group: `agentic-sre-dg`
- Lifecycle hooks: `appspec.yml` in repo root

### Slack App

| Property | Value |
|---|---|
| Interactivity URL | https://3sl5tgr7ri.execute-api.eu-north-1.amazonaws.com/prod/slack/events |
| Event Subscriptions URL | same |
| OAuth Scopes | channels:history, groups:history, chat:write, reactions:read |
| Review Channel | #agentic-sre-review (ID: C0B701B89BN) |
| Incidents Channel | #agentic-sre-incident (ID: C0B5XCQLP9C) |

---

## 5. The Flask Application

### Purpose
The Flask app running on EC2 is the **simulated production service** that generates real incidents for the SRE agent to investigate.

### File: `app/app.py`

The app has four routes:

| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check — always returns 200 |
| `/items` | GET | Main endpoint — triggers memory leak in Scenario 2 |
| `/items` | POST | Creates an item — always works |
| `/chaos` | GET | Returns 500 when `CHAOS_MODE=1` env var is set |

### Logging architecture

The app sends logs to **three destinations simultaneously**:

```
Flask log event
    ├── /var/log/flask-app/app.log (local file)
    ├── stdout → CloudWatch Logs via CloudWatch agent (/flask-app/logs)
    └── Splunk HEC → prd-p-uaboh.splunkcloud.com:8088 (index: main, source: ec2-flask-app)
```

**Two Splunk tokens exist — do not confuse them:**

| Token type | Value prefix | Used for | Used where |
|---|---|---|---|
| HEC token | `d8cf307b-...` | **Sending** logs TO Splunk | `app/app.py` env var `SPLUNK_HEC_TOKEN` |
| Bearer token | `73903edfe4...` | **Querying** logs FROM Splunk REST API | Lambda env var `SPLUNK_TOKEN` |

### Gunicorn configuration

The app runs with **2 worker processes** (important for understanding mixed 200/500 responses during load tests):

```bash
gunicorn \
  --workers 2 \
  --bind 0.0.0.0:5000 \
  --daemon \
  --pid /home/ec2-user/flask-app.pid \
  app:app
```

Each worker has its **own in-memory state**. During the memory leak scenario, each worker independently fills its cache, which is why you see alternating 200s and 500s during load testing — the workers hit the threshold at different times.

### Deployment lifecycle (CodeDeploy hooks)

```
appspec.yml defines:
  BeforeInstall  → app/scripts/before_install.sh   (create dirs, install deps)
  ApplicationStop → app/scripts/application_stop.sh (kill gunicorn via PID file)
  ApplicationStart → app/scripts/application_start.sh (start gunicorn daemon)
```

---

## 6. The Investigation Agent

### Overview

The agent is a **LangGraph StateGraph** — a directed graph where nodes are investigation functions that run in parallel, feed their findings into a shared state, and converge at an LLM synthesis node.

### File structure

```
investigation-agent/
├── handler.py              # Lambda entry point
├── slack_handler.py        # Lambda entry point for Slack button clicks
├── config.py               # LLM client factory
├── build_zip.py            # Builds lambda-package.zip (avoids Windows antivirus lock)
├── requirements.txt        # Python dependencies
├── .env                    # Local dev environment variables
├── agent/
│   ├── graph.py            # LangGraph graph wiring (fan-out / fan-in)
│   ├── state.py            # InvestigationState TypedDict
│   └── nodes/
│       ├── fetch_alarm.py      # Node 1: CloudWatch alarm details
│       ├── fetch_logs.py       # Node 2: CloudWatch Logs + Splunk
│       ├── fetch_metrics.py    # Node 3: CloudWatch GetMetricData
│       ├── fetch_cloudtrail.py # Node 4: CloudTrail LookupEvents
│       ├── fetch_github.py     # Node 5: GitHub commits + PRs
│       └── synthesise.py       # Node 6: LLM synthesis
├── storage/
│   └── dynamodb.py         # save_investigation, get_investigation, update_investigation
├── notifications/
│   └── slack.py            # post_hitl_review, post_to_incidents
└── infra/
    └── template.yaml       # CloudFormation for all AWS resources
```

### InvestigationState — the shared data structure

```python
class InvestigationState(TypedDict):
    # Input (set by handler.py before graph runs)
    incident: dict              # raw SQS payload from EventBridge
    incident_id: str            # unique UUID for DynamoDB PK

    # Populated by parallel investigation nodes
    alarm_details: dict         # CloudWatch alarm config + state history
    metrics: list[dict]         # ErrorCount, CPU datapoints
    logs: list[dict]            # normalised log entries (cloudwatch + splunk)
    cloudtrail_events: list[dict]
    github_commits: list[dict]  # commits + merged PRs

    # Synthesis output
    observations: list[str]     # one observation string per node
    root_cause: str
    mitigation_plan: str
    investigation_gaps: list[str]

    # Metadata
    status: str                 # investigating / pending_review / approved / dismissed
    approved_by: str | None
    slack_thread_ts: str | None
```

### LangGraph graph — fan-out / fan-in pattern

```
fetch_alarm (entry point)
      │
      ├──────────────────────────────────┐
      │         fan-out (parallel)       │
      ↓          ↓          ↓           ↓
fetch_logs  fetch_metrics  fetch_cloudtrail  fetch_github
      │          │              │               │
      └──────────┴──────────────┴───────────────┘
                        │
                    fan-in (all complete)
                        │
                    synthesise
                        │
                       END
```

The fan-out is implemented using `asyncio.gather` in the handler — all four parallel nodes are awaited concurrently, meaning the total investigation time is `max(node_times)` not `sum(node_times)`.

### Node details

#### `fetch_alarm.py`
- Calls `cloudwatch.describe_alarms(AlarmNames=[alarm_name])`
- Calls `cloudwatch.describe_alarm_history()` to get state transitions
- Extracts threshold, metric name, evaluation period, reason string
- Look-back: last 2 hours of alarm history

#### `fetch_logs.py`
- **CloudWatch Logs**: `logs.filter_log_events()` with pagination, max 200 events
- **Splunk REST API**: POST to `/services/search/jobs/export` with SPL query
  - Uses `exec_mode=oneshot` (blocking, returns results inline)
  - SSL verification disabled (Splunk Cloud uses self-signed cert)
  - Falls back from port 8089 to port 443 on timeout
  - Requires NAT Gateway fixed IP whitelisted in Splunk Cloud
- Time window: 30 minutes before incident, 5 minutes after

#### `fetch_metrics.py`
- Calls `cloudwatch.get_metric_data()` for `FlaskApp/ErrorCount`
- Also fetches CPU utilisation from EC2 instance metrics
- Time window: 30 minutes before incident

#### `fetch_cloudtrail.py`
- Calls `cloudtrail.lookup_events()` for the 2-hour window
- Filters for: CodeDeploy deployments, SSH sessions (`StartSession`), IAM changes
- Returns empty if no relevant events found (common in normal operation)

#### `fetch_github.py`
- Fetches commits to `master` branch: **6-hour look-back** (was 60 min — extended because memory leaks manifest hours after deployment)
- Fetches merged PRs: **24-hour look-back** (was 2 hours — extended for same reason)
- For each commit, fetches diff stats (files changed, additions, deletions)
- Uses GitHub REST API v3, stdlib `urllib` only (no PyGitHub dependency)

#### `synthesise.py`
- Formats all findings into a structured prompt
- Calls Claude Haiku via Anthropic API (configurable via `LLM_PROVIDER` env var)
- Expects JSON response with: `root_cause`, `mitigation_plan`, `investigation_gaps`
- After synthesis: saves to DynamoDB, posts Slack HITL card

### LLM configuration

`config.py` reads `LLM_PROVIDER` to select the client:

| LLM_PROVIDER | Model used | API key env var |
|---|---|---|
| `anthropic` | claude-haiku-4-5-20251001 | ANTHROPIC_API_KEY |
| `openai` | gpt-4o | OPENAI_API_KEY |
| `gemini` | (configurable) | GOOGLE_API_KEY |

**Default and recommended**: `anthropic` with `claude-haiku-4-5-20251001`.

### DynamoDB schema

Table: `sre-investigations`
- **PK**: `investigation_id` (UUID string, e.g. `inv-6710ef48-fd40-487d-bdb1-f42b94ef420e`)
- **SK**: `created_at` (ISO 8601 timestamp)

Key attributes stored per investigation:
```
alarm_name, status, root_cause, mitigation_plan,
observations[], investigation_gaps[],
raw_data (full JSON: metrics + logs + cloudtrail + github),
slack_thread_ts, approved_by,
llm_provider, llm_model,
created_at, published_at
```

---

## 7. The Slack HITL Flow

HITL = Human-in-the-Loop. The agent never auto-publishes — a human must approve.

### Flow diagram

```
Agent completes investigation
        ↓
post_hitl_review() → POST to Slack #agentic-sre-review
        ↓
Block Kit card appears:
  ┌─────────────────────────────────────────┐
  │ 🔍 New Investigation — flask-api-errors │
  │ ───────────────────────────────────────  │
  │ Impact: ErrorCount hit 32 (threshold 10) │
  │                                          │
  │ Root Cause:                              │
  │ Unbounded _item_cache in GET /items...   │
  │                                          │
  │ Mitigation Plan:                         │
  │ 1. Restart Flask to clear cache          │
  │ 2. Add LRU eviction policy...            │
  │                                          │
  │  [✅ Approve]        [❌ Dismiss]       │
  └─────────────────────────────────────────┘
        ↓ human clicks Approve
API Gateway → sre-slack-webhook Lambda
        ↓
  ├── Fetch full investigation from DynamoDB
  ├── Post full report to #agentic-sre-incident
  └── Update DynamoDB: status = "approved", approved_by = slack_user_id
```

### Button click handling

The `sre-slack-webhook` Lambda (`slack_handler.py`):
1. Validates Slack request signature using `SLACK_SIGNING_SECRET`
2. Parses the `payload` from the interactive component
3. Extracts `investigation_id` from the button `value` field
4. Routes to approve or dismiss handler
5. Returns 200 immediately (Slack requires response within 3 seconds)

---

## 8. Building From Scratch

This section walks through **exactly how to recreate this system** from a blank AWS account.

### Prerequisites

- AWS account with admin access (eu-north-1)
- GitHub account + repo
- Slack workspace with ability to create apps
- Splunk Cloud instance (optional — CloudWatch Logs works without it)
- Python 3.12 local environment
- AWS CLI configured (`aws configure`)

---

### Phase 1 — Flask App on EC2

**1.1 Launch EC2 instance**
- AMI: Amazon Linux 2023
- Instance type: t3.micro (sufficient for demo)
- IAM Role: create `agentic-sre-ec2-role` with policies:
  - `CloudWatchAgentServerPolicy`
  - `AmazonSSMManagedInstanceCore` (for SSM Session Manager access)
  - Custom inline: `cloudwatch:PutMetricData` on resource `*`
- Security Group: inbound TCP 5000 from your IP (or 0.0.0.0/0 for testing)

**1.2 Install app dependencies on EC2**
```bash
sudo yum install -y python3-pip
pip3 install flask gunicorn boto3
sudo mkdir -p /var/log/flask-app
sudo chown ec2-user:ec2-user /var/log/flask-app
```

**1.3 Install CloudWatch agent**
```bash
sudo yum install -y amazon-cloudwatch-agent
# Configure to stream /var/log/flask-app/app.log → /flask-app/logs log group
```

**1.4 Set up CodeDeploy + CodePipeline**
- Install CodeDeploy agent on EC2
- Create CodeDeploy application + deployment group targeting the instance
- Create CodePipeline: GitHub source → CodeDeploy deploy
- `appspec.yml` in repo root controls lifecycle hooks

**1.5 Deploy the Flask app**
The app at `app/app.py` will be deployed to `/home/ec2-user/` on EC2.
`application_start.sh` starts Gunicorn on port 5000 with 2 workers.

---

### Phase 2 — CloudWatch Alarm + EventBridge

**2.1 Create CloudWatch alarm**
- Metric: `FlaskApp/ErrorCount`
- Namespace: `FlaskApp`
- Statistic: Sum
- Period: 60 seconds
- Threshold: GreaterThanThreshold 10
- Evaluation periods: 1
- Name: `flask-api-errors`

> **Note**: The metric only appears in CloudWatch after the Flask app emits it at least once (first error). If the metric namespace doesn't exist yet, the alarm will show INSUFFICIENT_DATA.

**2.2 Create EventBridge rule**
- Event pattern: CloudWatch Alarm State Change → ALARM (see Section 4)
- Target: SQS queue (created in Phase 3)
- Add Input Transformer to format the event into the incident JSON payload

---

### Phase 3 — Deploy the CloudFormation Stack

The CloudFormation template creates all Lambda, SQS, DynamoDB, API Gateway, IAM, and VPC resources in one deployment.

**3.1 Build the Lambda package**

From `investigation-agent/`:
```powershell
# Windows PowerShell
Remove-Item -Recurse -Force package
New-Item -ItemType Directory -Force package | Out-Null
pip install -r requirements.txt -t package/ `
    --platform manylinux2014_x86_64 `
    --only-binary=:all: `
    --python-version 3.12 `
    --implementation cp `
    --no-cache-dir --quiet
Copy-Item -Recurse agent, storage, notifications -Destination package/
Copy-Item config.py, handler.py, slack_handler.py -Destination package/
python build_zip.py
```

Output: `lambda-package.zip`

**3.2 Upload to S3**
```bash
aws s3 cp lambda-package.zip s3://YOUR-BUCKET/investigation-agent/package.zip
```

**3.3 Deploy the stack**
```bash
aws cloudformation deploy \
  --template-file infra/template.yaml \
  --stack-name agentic-sre-investigation \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    LambdaS3Bucket=YOUR-BUCKET \
    LambdaS3Key=investigation-agent/package.zip \
    AnthropicApiKey=sk-ant-... \
    SlackBotToken=xoxb-... \
    SlackSigningSecret=... \
    SlackReviewChannel=C... \
    SlackIncidentsChannel=C... \
    GithubToken=ghp_... \
    GithubRepo=owner/repo \
    SplunkUrl=https://your.splunkcloud.com:8089 \
    SplunkToken=...
```

**3.4 Note the NAT Gateway Elastic IP from stack Outputs**
- Go to CloudFormation → Stack → Outputs → `NatGatewayElasticIP`
- Whitelist this IP in Splunk Cloud (Settings → Server settings → IP allow list → `/32`)

---

### Phase 4 — Slack App Configuration

**4.1 Create a Slack app** at api.slack.com/apps

**4.2 Add OAuth scopes** (Bot Token Scopes):
- `chat:write`
- `channels:history`
- `groups:history`
- `reactions:read`

**4.3 Enable Interactivity**
- URL: `https://YOUR-API-GW-ID.execute-api.eu-north-1.amazonaws.com/prod/slack/events`

**4.4 Enable Event Subscriptions**
- URL: same as above
- Subscribe to: `message.channels`

**4.5 Install app to workspace and copy**
- Bot User OAuth Token → `SLACK_BOT_TOKEN`
- Signing Secret → `SLACK_SIGNING_SECRET`

**4.6 Invite the bot to both channels**
```
/invite @your-bot-name
```

---

### Phase 5 — End-to-End Test

**5.1 Trigger the memory leak**
```powershell
while ($true) {
    try {
        Invoke-WebRequest -Uri "http://EC2-IP:5000/items" -UseBasicParsing | Out-Null
    } catch {}
    Start-Sleep -Milliseconds 500
}
```

**5.2 Watch the pipeline**
1. CloudWatch: ErrorCount metric appears → alarm transitions to ALARM
2. EventBridge: rule matches, sends to SQS
3. Lambda: triggered, runs all 5 investigation nodes in parallel
4. Slack: HITL review card appears in #agentic-sre-review
5. Click Approve: full report posted to #agentic-sre-incident
6. DynamoDB: record updated to status = "approved"

---

## 9. Environment Variables Reference

### `sre-investigation-agent` Lambda

| Variable | Example Value | Description |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | LLM backend: anthropic / openai / gemini |
| `LLM_MODEL` | `claude-haiku-4-5-20251001` | Model ID |
| `ANTHROPIC_API_KEY` | `sk-ant-api03-...` | Anthropic API key |
| `CLOUDWATCH_LOG_GROUP` | `/flask-app/logs` | Log group to search |
| `EC2_INSTANCE_ID` | `i-0dc405dc4a6d3c1eb` | EC2 instance for metric correlation |
| `GITHUB_TOKEN` | `ghp_...` | GitHub PAT with repo read access |
| `GITHUB_REPO` | `owner/repo` | GitHub repository |
| `GITHUB_BRANCH` | `master` | Branch to query commits on |
| `SPLUNK_URL` | `https://host.splunkcloud.com:8089` | Splunk REST API endpoint |
| `SPLUNK_TOKEN` | `73903edfe4...` | Splunk Bearer token for REST API queries |
| `SPLUNK_INDEX` | `main` | Splunk index to search |
| `SLACK_BOT_TOKEN` | `xoxb-...` | Slack Bot OAuth token |
| `SLACK_REVIEW_CHANNEL` | `C0B701B89BN` | Channel ID for HITL review |
| `SLACK_INCIDENTS_CHANNEL` | `C0B5XCQLP9C` | Channel ID for published reports |
| `DYNAMODB_TABLE` | `sre-investigations` | DynamoDB table name |

### `sre-slack-webhook` Lambda

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | Same as above |
| `SLACK_SIGNING_SECRET` | From Slack app Basic Information — used to verify request signatures |
| `SLACK_REVIEW_CHANNEL` | Same as above |
| `SLACK_INCIDENTS_CHANNEL` | Same as above |
| `DYNAMODB_TABLE` | Same as above |

### EC2 Flask app (set via `application_start.sh`)

| Variable | Description |
|---|---|
| `SPLUNK_HEC_URL` | `https://prd-p-uaboh.splunkcloud.com:8088` |
| `SPLUNK_HEC_TOKEN` | HEC token (different from REST API token) for sending logs |

---

## 10. Deployment Procedures

### Deploying a Lambda code change

1. Make code changes in `investigation-agent/`
2. Rebuild the zip (commands in Section 8 Phase 3.1)
3. Upload zip to S3:
   ```powershell
   aws s3 cp lambda-package.zip s3://YOUR-BUCKET/investigation-agent/package.zip
   ```
4. Update Lambda function code:
   ```bash
   aws lambda update-function-code \
     --function-name sre-investigation-agent \
     --s3-bucket YOUR-BUCKET \
     --s3-key investigation-agent/package.zip
   ```
   Or via console: Lambda → sre-investigation-agent → Code → Upload from S3.

### Deploying a Flask app change to EC2

1. Merge to `master` branch on GitHub
2. CodePipeline triggers automatically (source → deploy)
3. CodeDeploy runs lifecycle hooks: stop → deploy → start
4. Verify: `Invoke-WebRequest -Uri "http://EC2-IP:5000/health" -UseBasicParsing`

### Resetting the Flask app cache (for re-testing)

The memory leak cache (`_item_cache`) is module-level — it resets only on process restart.

Via SSM Session Manager (**EC2 → Connect → Session Manager**):
```bash
# Stop
PID_FILE=/home/ec2-user/flask-app.pid
if [ -f "$PID_FILE" ]; then kill "$(cat $PID_FILE)"; rm -f "$PID_FILE"; fi
fuser -k 5000/tcp 2>/dev/null || true

# Start
SPLUNK_HEC_URL=https://prd-p-uaboh.splunkcloud.com:8088
SPLUNK_HEC_TOKEN=$(grep SPLUNK_HEC_TOKEN /home/ec2-user/.bashrc | cut -d= -f2)
/home/ec2-user/.local/bin/gunicorn \
  --workers 2 --bind 0.0.0.0:5000 --daemon \
  --pid /home/ec2-user/flask-app.pid \
  --log-file /var/log/flask-app/gunicorn.log \
  --chdir /home/ec2-user \
  --env SPLUNK_HEC_URL=$SPLUNK_HEC_URL \
  --env SPLUNK_HEC_TOKEN=$SPLUNK_HEC_TOKEN \
  app:app
```

Verify restart: first `GET /items` should return HTTP 200 (cache is empty).

### Updating the CloudFormation stack

1. Edit `investigation-agent/infra/template.yaml`
2. In AWS console: CloudFormation → `agentic-sre-investigation` → Update
3. Select "Replace current template" → upload the file
4. On Parameters screen: tick "Use existing value" for all secrets
5. Create change set → review → Execute

---

## 11. Bug Scenarios

The project includes deliberately broken versions of `app/app.py` to test the investigation pipeline.

### Scenario 1 — Inventory service timeout (completed, archived)

**Bug**: `GET /items` calls a non-existent `inventory-service.internal` with a 0.001s timeout.
**Symptom**: 100% error rate immediately after deployment. DNS resolution failure.
**Detection**: CodeDeploy deployment + GitHub commit correlation.
**Branch**: historical (already merged and reverted)

### Scenario 2 — Memory leak (current)

**File**: `app/app.py` on branch `test/memory-leak` (merged to master)

**Bug**:
```python
_item_cache = {}   # module-level, never cleared

@app.route("/items")
def get_items():
    cache_key = f"items_{time.time()}"
    _item_cache[cache_key] = ["item-1", "item-2", "item-3"] * 1000  # 3000 items per key

    if len(_item_cache) > 100:
        logger.error("CRITICAL: Item cache exceeded safe limit")
        emit_error_metric()
        raise MemoryError(...)
```

**Symptom**: First 100 requests return 200. After that, each request adds to the cache AND raises MemoryError → HTTP 500.

**Why mixed 200/500?** Gunicorn runs 2 workers. Each has its own `_item_cache`. Requests round-robin across workers, so each worker hits the 100-entry threshold at a different time.

**How to trigger**:
```powershell
while ($true) {
    try {
        Invoke-WebRequest -Uri "http://EC2-IP:5000/items" -UseBasicParsing | Out-Null
    } catch {}
    Start-Sleep -Milliseconds 500
}
```

**Expected agent findings**:
- ErrorCount spike in metrics
- CRITICAL log entries in CloudWatch/Splunk
- PR #15 in GitHub (memory leak implementation)
- No CloudTrail events (no infra change — it's a code bug)

### Scenario 3 — Race condition (not yet implemented)

**File**: `app/scenario3_race.py` (currently empty)
**Planned**: Concurrent writes to a shared resource without locking — manifests as intermittent data corruption errors.

---

## 12. Comparison: Custom Agent vs AWS DevOps Agent

### On Scenario 1 (inventory-service timeout)

**AWS DevOps Agent found**:
- Deployment correlation: CodeDeploy `d-OQXUNOVLH` at 11:36 UTC (3 min before errors)
- DNS resolution failure in logs: `socket.gaierror: [Errno -2]`
- Commit hash: `9475560`
- IAM user monitoring activity

**Custom agent initially missed**:
- GitHub PR/commit correlation (look-back window too short — only 60 min for commits, 2 hours for PRs)

**Fix applied**: Extended windows to 6 hours (commits) and 24 hours (PRs).

### Key insight

Memory leaks and race conditions manifest **hours after the bad code is deployed**. A 1-hour commit window will miss the causing deployment entirely. The 24-hour PR window ensures even slow-manifesting bugs get their root cause traced back to the code change.

### Evaluation dimensions

| Dimension | AWS DevOps Agent | Custom Agent |
|---|---|---|
| Deployment correlation | Excellent | Good (after window fix) |
| Log analysis | Good | Good (CloudWatch + Splunk) |
| Metric correlation | Good | Good |
| Mitigation plan quality | Detailed | Depends on prompt quality |
| Customisability | None | Full |
| Cost | Per-investigation pricing | LLM API cost only |
| Investigation history | Agent Space console | DynamoDB (queryable) |
| HITL flow | Agent Space UI | Slack (team's primary tool) |

---

## 13. Known Issues & Lessons Learned

### Splunk connectivity from Lambda

**Problem**: Splunk Cloud firewalls port 8089 to specific IPs. Lambda's default outbound IP is dynamic (changes per cold start).

**Solution**: VPC + NAT Gateway with a fixed Elastic IP (`13.63.151.169`). Whitelist that IP in Splunk Cloud.

**Caveat**: When Lambda is placed in a VPC private subnet, it **loses direct access to AWS services** (DynamoDB, SQS, CloudWatch, etc.) unless:
- A NAT Gateway routes traffic to AWS public endpoints, OR
- VPC Endpoints are configured for each service

This project uses the NAT Gateway approach — simpler and covers all services.

### GitHub commit look-back too short

**Problem**: Memory leaks manifest hours after deployment. A 60-minute commit window misses the causing commit.

**Fix**: Extended to 6 hours for commits, 24 hours for PRs (`fetch_github.py`).

### LLM provider misconfiguration

**Problem**: Lambda had `LLM_PROVIDER=openai` with `OPENAI_API_KEY=123abc` (placeholder) from a previous test deployment. CloudFormation "Use existing value" preserved these wrong values during the VPC update.

**Lesson**: After any CloudFormation stack update, verify LLM-related env vars in the Lambda console.

### EC2 public IP is dynamic

**Problem**: The EC2 instance IP (51.20.6.149) changes if the instance is stopped and restarted.

**Lesson**: Always check the current IP in EC2 console before running load tests. Consider assigning an Elastic IP to the EC2 instance for stability.

### Gunicorn workers each have independent state

**Problem**: During load testing, you see alternating 200s and 500s after the cache threshold is crossed, which can be confusing.

**Explanation**: With 2 workers, each has its own `_item_cache`. Worker A might have 105 entries (error) while Worker B has 98 (success). Requests round-robin → mixed responses.

**Lesson**: To get all-500s consistently, run enough requests to fill both workers' caches (200+ requests).

---

## 14. Troubleshooting Runbook

### Alarm fires but Lambda not triggered

1. Check EventBridge rule `sre-cloudwatch-alarm-to-sqs` is **enabled**
2. Check SQS queue `sre-investigation-queue` for messages (may be stuck)
3. Check Lambda's SQS event source mapping is enabled
4. Check Lambda is not in error state from a previous invocation

### Lambda triggered but no Slack message

1. Check CloudWatch Logs `/aws/lambda/sre-investigation-agent` for errors
2. Common causes:
   - `synthesise` node LLM error (wrong API key, wrong provider)
   - Slack `post_hitl_review()` failed (wrong channel ID, bot not in channel)
   - DynamoDB save failed (permissions issue)

### Lambda triggered but using wrong LLM

Check Lambda env vars:
- `LLM_PROVIDER` must be `anthropic`
- `LLM_MODEL` must be `claude-haiku-4-5-20251001`
- `ANTHROPIC_API_KEY` must be the real key (not a placeholder)

### Splunk returning no results

1. Verify NAT Gateway IP (13.63.151.169) is whitelisted in Splunk Cloud
2. Check `SPLUNK_URL` includes port: `https://host.splunkcloud.com:8089`
3. Check `SPLUNK_TOKEN` is the **Bearer token** (not the HEC token)
4. Check the time window in `fetch_logs.py` covers when errors occurred

### GitHub returning no commits/PRs

1. Verify `GITHUB_TOKEN` is valid and has `repo` read scope
2. Verify `GITHUB_REPO` format is `owner/repo` (no https://)
3. Check the look-back window (6h commits, 24h PRs) covers the incident time
4. Check `GITHUB_BRANCH` matches the branch commits were pushed to

### Flask app not responding

1. Check EC2 IP hasn't changed: EC2 console → Instance details → Public IPv4
2. Check port 5000 is open in the Security Group inbound rules
3. Check Gunicorn is running:
   - Via SSM: `cat /home/ec2-user/flask-app.pid` then `ps -p <pid>`
4. Check logs: `tail -100 /var/log/flask-app/app.log`

### CloudWatch metric `FlaskApp/ErrorCount` not appearing

1. Verify EC2 IAM role (`agentic-sre-ec2-role`) has `cloudwatch:PutMetricData` permission
2. Verify `/items` is actually returning 500s (run the load test and check status codes)
3. Wait 2–3 minutes — CloudWatch has ingestion delay

---

## 15. Future Roadmap

| Task | Priority | Notes |
|---|---|---|
| Scenario 3 — race condition | High | Write `app/scenario3_race.py` |
| Splunk whitelist confirmation | High | Confirm 13.63.151.169 is approved |
| Dismiss button end-to-end test | Medium | Verify DynamoDB status = "dismissed" |
| Investigation quality improvements | Medium | Improve synthesis prompt, add more context |
| LLM provider comparison | Medium | Run same incident through Haiku vs GPT-4o |
| Side-by-side comparison report | Medium | Formal benchmark: Custom vs AWS DevOps Agent |
| AWS DevOps Agent disable | Low | After custom agent quality is confirmed |
| Elastic IP for EC2 | Low | Prevent IP change on instance restart |
| Multi-region support | Future | Extend to eu-west-1 or us-east-1 |
| Automated scenario runner | Future | Script that triggers scenarios and captures both agents' outputs |

---

## Quick Reference Card

```
Re-trigger memory leak:
  PowerShell: while ($true) { try { iwr http://51.20.6.149:5000/items -UseBasicParsing | Out-Null } catch {} ; sleep -ms 500 }

Reset Flask app cache:
  EC2 SSM → kill $(cat /home/ec2-user/flask-app.pid) && fuser -k 5000/tcp 2>/dev/null; then restart gunicorn

Check investigation running:
  CloudWatch Logs → /aws/lambda/sre-investigation-agent → latest log stream

Manually trigger Lambda test:
  Lambda console → sre-investigation-agent → Test → use event from PROJECT_SUMMARY.md Section 9

Check DynamoDB investigation records:
  DynamoDB → sre-investigations → Explore items

Toggle custom agent on/off:
  EventBridge → Rules → sre-cloudwatch-alarm-to-sqs → Enable/Disable

Toggle AWS DevOps Agent on/off:
  aidevops.global.app.aws → agentic-sre-agent → disable CloudWatch integration

Build Lambda zip:
  cd investigation-agent
  Remove-Item -Recurse -Force package; mkdir package
  pip install -r requirements.txt -t package/ --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12 --implementation cp --no-cache-dir
  Copy-Item -Recurse agent, storage, notifications -Destination package/
  Copy-Item config.py, handler.py, slack_handler.py -Destination package/
  python build_zip.py
```
