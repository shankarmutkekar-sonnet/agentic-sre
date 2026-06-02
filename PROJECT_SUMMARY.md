# Agentic SRE — Project Summary & Briefing Document

> Use this document to onboard a new Claude conversation to the full project context.
> Last updated: 2026-06-01

---

## 1. Project Goal

Build a **custom LangGraph-based SRE investigation agent** that:
- Automatically detects CloudWatch alarms
- Runs parallel investigations (logs, metrics, CloudTrail, GitHub commits)
- Synthesises findings with Claude Haiku (LLM)
- Posts a HITL (Human-in-the-Loop) review card to Slack with Approve/Dismiss buttons
- On approval, publishes a full incident report to a Slack incidents channel
- Persists all investigations to DynamoDB

Future goal: Compare this agent's reports against **AWS DevOps Guru** for the same bug scenarios.

---

## 2. End-to-End Architecture

```
EC2 Flask App
  │
  ├── Sends logs → CloudWatch Logs (/flask-app/logs)
  ├── Sends logs → Splunk HEC (source: ec2-flask-app, index: main)
  └── Emits ErrorCount metric → CloudWatch Metrics (Namespace: FlaskApp)

CloudWatch Alarm (SRE-ErrorRate)
  │
  └── ALARM state → EventBridge rule (sre-cloudwatch-alarm-to-sqs)
        │  [Input Transformer formats alarm to incident JSON]
        └── SQS queue (sre-investigation-queue)
              │
              └── sre-investigation-agent Lambda (triggered by SQS)
                    │
                    ├── Parallel nodes (asyncio.gather):
                    │     ├── fetch_alarm      — CloudWatch alarm details
                    │     ├── fetch_logs       — CloudWatch Logs + Splunk REST API
                    │     ├── fetch_metrics    — CloudWatch GetMetricData
                    │     ├── fetch_cloudtrail — CloudTrail LookupEvents
                    │     └── fetch_github     — GitHub commits on master
                    │
                    ├── LLM synthesis node — Claude Haiku (root_cause + mitigation_plan)
                    │
                    ├── DynamoDB save (sre-investigations table)
                    │
                    └── Slack HITL review card → #agentic-sre-review
                          │  [Block Kit with Approve / Dismiss buttons]
                          │
                          └── Button click → API Gateway → sre-slack-webhook Lambda
                                │
                                ├── Approve → posts full report to #agentic-sre-incident
                                │            updates DynamoDB status = "approved"
                                └── Dismiss → updates DynamoDB status = "dismissed"
```

---

## 3. AWS Infrastructure

### Region
All resources: **eu-north-1** (Stockholm)

### EC2
| Item | Value |
|---|---|
| Instance ID | i-0dc405dc4a6d3c1eb |
| Public IP | 51.20.6.149 (dynamic — may change on restart) |
| App | Flask API on port 5000 |
| Deployment | AWS CodeDeploy + CodePipeline from GitHub master branch |
| Log path | /var/log/flask-app/app.log |
| CloudWatch agent | Streams to /flask-app/logs log group |

### CloudFormation Stack
Stack name: **agentic-sre-investigation**

| Resource | Name/ID |
|---|---|
| Investigation Lambda | sre-investigation-agent (900s timeout, 512MB, Python 3.12) |
| Slack Webhook Lambda | sre-slack-webhook (30s timeout, 256MB, Python 3.12) |
| DynamoDB Table | sre-investigations (PK: investigation_id, SK: created_at) |
| SQS Queue | sre-investigation-queue (VisibilityTimeout: 960s) |
| SQS DLQ | sre-investigation-dlq (14 day retention, maxReceiveCount: 2) |
| API Gateway | sre-slack-events-api |
| API Gateway URL | https://3sl5tgr7ri.execute-api.eu-north-1.amazonaws.com/prod/slack/events |
| IAM Role (agent) | sre-investigation-lambda-role |
| IAM Role (slack) | sre-slack-webhook-lambda-role |

### EventBridge Rule
Rule name: **sre-cloudwatch-alarm-to-sqs**  
Target: sre-investigation-queue

Input path:
```json
{
  "alarmName": "$.detail.alarmName",
  "state":     "$.detail.state.value",
  "reason":    "$.detail.state.reason",
  "region":    "$.region",
  "account":   "$.account",
  "time":      "$.time"
}
```

Input template:
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

### CloudWatch
| Item | Value |
|---|---|
| Log Group | /flask-app/logs |
| Alarm | SRE-ErrorRate (triggers on FlaskApp/ErrorCount > threshold) |

### CodePipeline / CodeDeploy
- Source: GitHub repo master branch
- Deploy target: EC2 instance i-0dc405dc4a6d3c1eb
- appspec.yml in repo root controls deployment lifecycle

---

## 4. Environment Variables

### investigation-agent/.env (local dev)

```ini
# LLM
LLM_PROVIDER=anthropic
LLM_MODEL=claude-haiku-4-5-20251001
ANTHROPIC_API_KEY=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
GOOGLE_API_KEY=AQ.Ab8RN6JcVYrJ5WJqCwiYpcvVOwyB9PkM3e6P-oyCwa3D881A6A

# AWS
AWS_REGION=eu-north-1
EC2_INSTANCE_ID=i-0dc405dc4a6d3c1eb
CLOUDWATCH_LOG_GROUP=/flask-app/logs

# GitHub
GITHUB_TOKEN=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
GITHUB_REPO=shankarmutkekar-sonnet/agentic-sre
GITHUB_BRANCH=master

# Splunk (REST API query — Bearer token, NOT HEC token)
SPLUNK_URL=https://prd-p-uaboh.splunkcloud.com:8089
SPLUNK_TOKEN=73903edfe41286fcbb3f33f57fa2fd125eb2b381bf7ddfc2affca60be573af52
SPLUNK_INDEX=main

# Slack
SLACK_BOT_TOKEN=xoxb-797224974471-11232390408006-JwfQ10AOCiO9s9tMuuD1GBx8
SLACK_REVIEW_CHANNEL=C0B701B89BN
SLACK_INCIDENTS_CHANNEL=C0B5XCQLP9C
SLACK_SIGNING_SECRET=aa82875d96a26c91dbc99da4017f2e7d

# DynamoDB
DYNAMODB_TABLE=sre-investigations
```

### Lambda Environment Variables (sre-investigation-agent)
Same as above except:
- `AWS_REGION` → set automatically by Lambda runtime
- `SLACK_SIGNING_SECRET` → not needed (only sre-slack-webhook needs it)

### Lambda Environment Variables (sre-slack-webhook)
```
SLACK_BOT_TOKEN=xoxb-797224974471-11232390408006-JwfQ10AOCiO9s9tMuuD1GBx8
SLACK_SIGNING_SECRET=aa82875d96a26c91dbc99da4017f2e7d
SLACK_REVIEW_CHANNEL=C0B701B89BN
SLACK_INCIDENTS_CHANNEL=C0B5XCQLP9C
DYNAMODB_TABLE=sre-investigations
```

### Splunk Token Distinction (IMPORTANT)
There are **two separate Splunk tokens**:

| Token | Value | Used for | Used by |
|---|---|---|---|
| HEC token | `d8cf307b-b62a-47aa-9c4b-687159bec523` | **Sending** logs TO Splunk (`Authorization: Splunk <token>`) | `app/app.py` via `SPLUNK_HEC_TOKEN` on EC2 |
| Bearer token | `73903edfe41286fcbb3f33f57fa2fd125eb2b381bf7ddfc2affca60be573af52` | **Querying** logs FROM Splunk REST API (`Authorization: Bearer <token>`) | `fetch_logs.py` via `SPLUNK_TOKEN` in Lambda |

---

## 5. Key Source Files

```
agentic-sre/
├── app/
│   ├── app.py                    Flask app (master = baseline; feature/add-bug = memory leak scenario)
│   ├── requirements.txt
│   └── scripts/
│       ├── application_start.sh
│       ├── application_stop.sh
│       └── before_install.sh
├── appspec.yml                   CodeDeploy deployment spec
└── investigation-agent/
    ├── handler.py                Lambda entry point (SQS trigger → runs LangGraph graph)
    ├── slack_handler.py          Lambda entry point for Slack webhook (button clicks)
    ├── config.py                 Shared config / LLM client factory
    ├── build_zip.py              Python script to build Lambda zip (avoids Windows antivirus lock)
    ├── requirements.txt          Python dependencies
    ├── .env                      Local dev environment variables
    ├── agent/
    │   ├── graph.py              LangGraph StateGraph definition (fan-out/fan-in)
    │   ├── state.py              InvestigationState TypedDict
    │   └── nodes/
    │       ├── fetch_alarm.py    Fetches CloudWatch alarm details
    │       ├── fetch_logs.py     Fetches CloudWatch Logs + Splunk (parallel)
    │       ├── fetch_metrics.py  Fetches CloudWatch metrics
    │       ├── fetch_cloudtrail.py  CloudTrail LookupEvents
    │       ├── fetch_github.py   GitHub commits around incident time
    │       └── synthesise.py     LLM synthesis node (root_cause + mitigation_plan)
    ├── storage/
    │   └── dynamodb.py           DynamoDB read/write (save_investigation, get_investigation, update_investigation)
    ├── notifications/
    │   └── slack.py              Slack posting (post_hitl_review, post_to_incidents)
    └── infra/
        └── template.yaml         CloudFormation template for all AWS resources
```

---

## 6. What Works (Confirmed)

- Full investigation pipeline: CloudWatch Alarm → EventBridge → SQS → Lambda → investigation runs
- All parallel investigation nodes execute concurrently (asyncio.gather)
- CloudWatch Logs fetching from /flask-app/logs
- CloudWatch metrics fetching
- GitHub commits fetching
- LLM synthesis (Claude Haiku via Anthropic API)
- DynamoDB save (status: pending_review)
- Slack HITL review card posted to #agentic-sre-review with Block Kit buttons
- **Approve button**: posts full investigation report to #agentic-sre-incident, updates DynamoDB status → "approved"
- Live pipeline: real CloudWatch alarm automatically triggers investigation (no manual Lambda invocation needed)
- CodeDeploy baseline deployment to EC2 (app/app.py baseline)

---

## 7. Known Issues & Pending Tasks

### Splunk connectivity from Lambda — BLOCKED
- **Symptom**: Port 8089 → timeout; Port 443 → HTTP 404
- **Root cause**: Splunk Cloud firewalls port 8089 from dynamic IPs. Lambda has no fixed outbound IP.
- **Fix**: Put Lambda in a VPC with a NAT Gateway (fixed Elastic IP) → whitelist that IP in Splunk Cloud
- CloudWatch Logs are working fine as a fallback in the meantime.

### Pending work
| Task | Status |
|---|---|
| VPC + NAT Gateway for Lambda→Splunk | **Next task** |
| Scenario 2 (memory leak): deploy app/app.py from feature/add-bug branch | Not started |
| Scenario 3 (race condition): write scenario3_race.py (currently empty) | Not started |
| Dismiss button end-to-end test | Deferred |
| AWS DevOps Guru setup (for comparison) | Future |

---

## 8. Next Task: VPC + NAT Gateway for Lambda

**Goal**: Give `sre-investigation-agent` Lambda a fixed outbound IP so it can reach Splunk Cloud port 8089.

**Steps (console-preferred)**:
1. Create a VPC (or use the default VPC if it has internet gateway)
2. Create a private subnet + public subnet in the VPC
3. Create an Internet Gateway (attach to VPC if not already done)
4. Create a NAT Gateway in the public subnet, assign an Elastic IP
5. Create a route table for the private subnet: `0.0.0.0/0 → NAT Gateway`
6. Update `sre-investigation-agent` Lambda → Configuration → VPC:
   - Select the VPC
   - Select the **private** subnet(s)
   - Assign a security group that allows outbound HTTPS (443) and port 8089
7. Note the **Elastic IP** of the NAT Gateway
8. Whitelist that Elastic IP in Splunk Cloud (Settings → Server settings → IP allow list, or raise a support ticket)
9. Test: invoke Lambda with a test event and check CloudWatch logs for `[fetch_logs] X Splunk entries`

**Important caveat**: When Lambda is placed in a VPC, it loses direct access to AWS services unless the VPC has either:
- A NAT Gateway (for public internet + AWS public endpoints), OR
- VPC Endpoints for each AWS service (CloudWatch, DynamoDB, SQS, etc.)

Using NAT Gateway covers everything and is the simpler path.

---

## 9. Lambda Test Event

Use this to manually trigger an investigation from the Lambda console Test tab:

```json
{
  "Records": [
    {
      "body": "{\"title\":\"CloudWatch Alarm: SRE-ErrorRate\",\"service\":\"ec2-flask-api\",\"action\":\"created\",\"priority\":\"HIGH\",\"timestamp\":\"2026-06-01T05:00:00Z\",\"data\":{\"alarmName\":\"SRE-ErrorRate\",\"state\":\"ALARM\",\"reason\":\"Threshold crossed\",\"region\":\"eu-north-1\",\"account\":\"your-account-id\"}}"
    }
  ]
}
```

> Adjust `timestamp` to within the last hour so the 30-minute log fetch window covers recent EC2 activity.

---

## 10. Lambda Package Build Commands (Windows PowerShell)

Run from `D:\Projects\agentic-sre\investigation-agent\`:

```powershell
Remove-Item -Recurse -Force package; New-Item -ItemType Directory -Force package | Out-Null
pip install -r requirements.txt -t package/ --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12 --implementation cp --no-cache-dir --quiet
Copy-Item -Recurse agent, storage, notifications -Destination package/
Copy-Item config.py, handler.py, slack_handler.py -Destination package/
python build_zip.py
```

Output: `lambda-package.zip` in the `investigation-agent/` directory.

Upload to S3, then update Lambda function code via console (Upload from S3).

---

## 11. Slack App Configuration

- **App name**: (your Slack app)
- **Interactivity & Shortcuts URL**: `https://3sl5tgr7ri.execute-api.eu-north-1.amazonaws.com/prod/slack/events`
- **Event Subscriptions URL**: same URL (verified ✓)
- **Subscribed bot events**: `reaction_added` (note: not actually delivered — using buttons instead), `message.channels`
- **OAuth Scopes**: `channels:history`, `groups:history`, `chat:write`, `reactions:read`
- **Channels**:
  - `#agentic-sre-review` → Channel ID: C0B701B89BN (SLACK_REVIEW_CHANNEL)
  - `#agentic-sre-incident` → Channel ID: C0B5XCQLP9C (SLACK_INCIDENTS_CHANNEL)

---

## 12. Git Branches

| Branch | Purpose |
|---|---|
| master | Production — baseline Flask app |
| feature/add-bug | Scenario 2: memory leak bug in app/app.py |

Current status of `feature/add-bug`: `app/app.py` has the memory leak scenario written but not yet merged/deployed.
