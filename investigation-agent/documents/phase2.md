# Phase 2 — Persistence, HITL Slack Review & Infrastructure

## What Phase 2 adds

Phase 1 built the investigation pipeline and proved it works end-to-end.
Phase 2 closes the loop:

| Concern | Phase 1 | Phase 2 |
|---------|---------|---------|
| Investigation storage | Stub (log only) | **DynamoDB** (`sre-investigations`) |
| Human review | Stub (log only) | **Slack HITL** — react ✅/❌ in `#agentic-sre-review` |
| Publishing | — | Approved reports posted to `#agentic-sre-incident` |
| Slack webhook | — | **Separate Lambda** + API Gateway for reaction events |
| Infrastructure | Manual | **CloudFormation** `infra/template.yaml` |

---

## New files

```
investigation-agent/
├── storage/
│   └── dynamodb.py          ← DynamoDB read/write helpers
├── notifications/
│   └── slack.py             ← Slack Block Kit messages + HITL logic
├── slack_handler.py         ← Lambda entry point for Slack Events API
├── infra/
│   └── template.yaml        ← CloudFormation (Lambda × 2, DynamoDB, SQS, APIGW)
└── documents/
    └── phase2.md            ← this file
```

### Changed files

| File | Change |
|------|--------|
| `handler.py` | Replaced `_save_to_dynamodb` and `_post_to_slack` stubs with real calls |
| `requirements.txt` | Added `slack_sdk>=3.27.0` |

---

## Module deep-dives

### `storage/dynamodb.py`

**Table schema**

| Attribute | Type | Role |
|-----------|------|------|
| `investigation_id` | String | Partition key — UUID (`inv-<uuid4>`) |
| `created_at` | String | Sort key — ISO-8601 timestamp |
| `alarm_name` | String | Extracted alarm name |
| `status` | String | `investigating` → `pending_review` → `published` / `dismissed` |
| `root_cause` | String | LLM output, capped at 10,000 chars |
| `mitigation_plan` | String | LLM output, capped at 10,000 chars |
| `slack_thread_ts` | String | Slack message ts — used to match reaction events |
| `approved_by` | String | Slack user ID of the approver |
| `published_at` | String | ISO-8601 timestamp of publication |
| `raw_data` | Map | Capped evidence: metrics (≤60), logs (≤100), cloudtrail (≤50) |

**Key functions**

```python
save_investigation(state)                  # Called once after graph completes
update_investigation(id, {"status": ...})  # Called by handler + slack_handler
get_investigation(id)                      # Retrieves by investigation_id PK
```

**Size management** — DynamoDB items are limited to 400 KB.  
The `raw_data` map caps sub-collections and strings to stay well clear of that limit.  
Floats are converted to `Decimal` with `_to_decimal()` because DynamoDB rejects Python floats.

---

### `notifications/slack.py`

**HITL flow**

```
Investigation complete
        │
        ▼
post_hitl_review(state)
  → Posts Block Kit card to #agentic-sre-review
  → Returns Slack message ts
        │
        ▼
handler.py stores ts in DynamoDB (slack_thread_ts)
        │
        ▼  (SRE engineer reacts)
        ┌──────────────────────────────────────────┐
        │ ✅ white_check_mark                       │
        │   → post_to_incidents(state, user_id)    │
        │   → update DynamoDB: status=published     │
        └──────────────────────────────────────────┘
        ┌──────────────────────────────────────────┐
        │ ❌ x                                      │
        │   → update DynamoDB: status=dismissed    │
        └──────────────────────────────────────────┘
```

**Block Kit card (review)**

```
┌──────────────────────────────────────────────────────┐
│ 🔍 New Investigation — flask-api-errors              │
├──────────────────────────────────────────────────────┤
│ Alarm            │ Triggered        │ Priority        │
│ flask-api-errors │ 2025-06-01T…    │ HIGH            │
│ Investigation ID: inv-3f2a…                          │
├──────────────────────────────────────────────────────┤
│ Root Cause                                           │
│ Elevated 5xx error rate …                            │
├──────────────────────────────────────────────────────┤
│ Mitigation Plan                                      │
│ 1. Check recent deploys …                            │
├──────────────────────────────────────────────────────┤
│ React ✅ to approve and publish to #agentic-sre-incident │
│ React ❌ to dismiss this investigation               │
└──────────────────────────────────────────────────────┘
```

---

### `slack_handler.py`

Separate Lambda, invoked via **API Gateway POST /slack/events**.

**Event routing**

| Slack event type | Action |
|-----------------|--------|
| `url_verification` | Echo the `challenge` back (required on first setup) |
| `event_callback` → `reaction_added` | Call `handle_reaction_event(body)` |
| anything else | Return 200 ignored |

**Signature verification** (`_verify_slack_signature`)

Uses `HMAC-SHA256` over `v0:{timestamp}:{body}` with `SLACK_SIGNING_SECRET`.  
Requests older than 5 minutes are rejected (replay-attack guard).

---

### `infra/template.yaml`

**Resources deployed**

| Resource | Type | Purpose |
|----------|------|---------|
| `InvestigationsTable` | DynamoDB Table | Investigation history store |
| `InvestigationQueue` | SQS Queue | Buffers incidents for the investigation Lambda |
| `InvestigationDLQ` | SQS Queue | Dead-letter queue (2 receive attempts, 14-day retention) |
| `InvestigationFunction` | Lambda | Runs the LangGraph pipeline (`handler.handler`) |
| `InvestigationSQSTrigger` | Lambda ESM | SQS → Lambda, batch size 1 |
| `SlackWebhookFunction` | Lambda | Handles Slack reactions (`slack_handler.handler`) |
| `SlackApiGateway` | API Gateway | HTTPS endpoint for Slack Events API |
| `InvestigationLambdaRole` | IAM Role | CW metrics/logs, CloudTrail, DynamoDB, SQS |
| `SlackLambdaRole` | IAM Role | DynamoDB query/scan/update only |

**Deployment command (after packaging)**

```bash
# 1. Package (from investigation-agent/)
pip install -r requirements.txt -t package/
cd package && zip -r ../package.zip . && cd ..
zip -g package.zip handler.py slack_handler.py config.py \
    agent/ storage/ notifications/

# 2. Upload to S3
aws s3 cp package.zip s3://<your-bucket>/investigation-agent/package.zip

# 3. Deploy / update stack
aws cloudformation deploy \
  --template-file infra/template.yaml \
  --stack-name agentic-sre-investigation \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    LambdaS3Bucket=<your-bucket> \
    LlmProvider=anthropic \
    LlmModel=claude-haiku-4-5-20251001 \
    AnthropicApiKey=$ANTHROPIC_API_KEY \
    GithubToken=$GITHUB_TOKEN \
    GithubRepo=owner/repo \
    GithubBranch=master \
    SplunkUrl=$SPLUNK_URL \
    SplunkToken=$SPLUNK_TOKEN \
    SlackBotToken=$SLACK_BOT_TOKEN \
    SlackSigningSecret=$SLACK_SIGNING_SECRET \
    SlackReviewChannel=C0B701B89BN \
    SlackIncidentsChannel=C0B5XCQLP9C \
    Ec2InstanceId=i-0dc405dc4a6d3c1eb \
    CloudwatchLogGroup=/flask-app/logs

# 4. Retrieve the Slack webhook URL from outputs
aws cloudformation describe-stacks \
  --stack-name agentic-sre-investigation \
  --query 'Stacks[0].Outputs[?OutputKey==`SlackWebhookUrl`].OutputValue' \
  --output text
```

Then paste the URL into your Slack app's **Event Subscriptions → Request URL** field.

---

## End-to-end Phase 2 flow

```
CloudWatch Alarm fires
        │
        ▼
EventBridge rule → SQS (InvestigationQueue)
        │
        ▼
handler.handler (Lambda, 15 min timeout)
  ├── LangGraph investigation graph
  │     ├── fetch_alarm        (sequential)
  │     ├── fetch_metrics      ┐
  │     ├── fetch_logs         ├── parallel (asyncio)
  │     ├── fetch_cloudtrail   │
  │     └── fetch_github       ┘
  │     └── synthesize         (sequential, LLM call)
  │
  ├── _save_to_dynamodb(final_state)
  │     → DynamoDB: status=pending_review
  │
  └── _post_to_slack(final_state)
        → Slack: Block Kit card to #agentic-sre-review
        → DynamoDB: update slack_thread_ts
        │
        ▼ (SRE engineer reacts ✅)
slack_handler.handler (Lambda, 30 s timeout)
  ← API Gateway POST /slack/events
  ├── _verify_slack_signature()
  ├── handle_reaction_event(body)
  │     ├── DynamoDB scan by slack_thread_ts
  │     ├── post_to_incidents(state, user_id)
  │     │     → Slack: approved report to #agentic-sre-incident
  │     └── DynamoDB update: status=published, approved_by, published_at
  └── return 200 published
```

---

## Environment variables (full list)

| Variable | Used by | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | config.py | `anthropic` / `openai` / `gemini` |
| `LLM_MODEL` | config.py | Model ID |
| `ANTHROPIC_API_KEY` | config.py | Anthropic key |
| `OPENAI_API_KEY` | config.py | OpenAI key |
| `GOOGLE_API_KEY` | config.py | Google key |
| `AWS_REGION` | dynamodb.py, nodes | `eu-north-1` |
| `EC2_INSTANCE_ID` | fetch_metrics.py | Fallback if not in alarm dims |
| `CLOUDWATCH_LOG_GROUP` | fetch_logs.py | CW Logs group to search |
| `GITHUB_TOKEN` | fetch_github.py | PAT for GitHub API |
| `GITHUB_REPO` | fetch_github.py | `owner/repo` |
| `GITHUB_BRANCH` | fetch_github.py | Branch (default: `master`) |
| `SPLUNK_URL` | fetch_logs.py | Splunk REST endpoint |
| `SPLUNK_TOKEN` | fetch_logs.py | Splunk API token |
| `SPLUNK_INDEX` | fetch_logs.py | Splunk index |
| `SLACK_BOT_TOKEN` | slack.py | `xoxb-...` bot token |
| `SLACK_SIGNING_SECRET` | slack_handler.py | Webhook signature verification |
| `SLACK_REVIEW_CHANNEL` | slack.py | `#agentic-sre-review` channel ID |
| `SLACK_INCIDENTS_CHANNEL` | slack.py | `#agentic-sre-incident` channel ID |
| `DYNAMODB_TABLE` | dynamodb.py | Table name (`sre-investigations`) |

---

## Slack app setup checklist

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → select your app
2. **OAuth & Permissions** → Bot Token Scopes:
   - `chat:write`
   - `reactions:read`
   - `channels:history` (to read reaction context)
3. **Event Subscriptions** → Enable Events → Request URL:  
   `https://<api-id>.execute-api.eu-north-1.amazonaws.com/prod/slack/events`
4. Subscribe to **bot events**: `reaction_added`
5. **Install app to workspace** and copy `Bot User OAuth Token` → `SLACK_BOT_TOKEN`
6. **Basic Information** → Signing Secret → `SLACK_SIGNING_SECRET`
7. Invite the bot to both `#agentic-sre-review` and `#agentic-sre-incident`

---

## What Phase 3 will cover

- End-to-end integration test (live alarm → DynamoDB → Slack → ✅ → #incidents)
- Splunk connectivity from Lambda (VPC / PrivateLink or public endpoint)
- LLM provider failover (Anthropic → OpenAI on rate-limit)
- Side-by-side comparison with the original AWS DevOps Agent
- Cost and latency benchmarking
