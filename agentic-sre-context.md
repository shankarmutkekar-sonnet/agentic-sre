# Agentic SRE — Project Context for Claude Code

Paste this file into your Claude Code session to continue development without losing any context.

---

## What we are building

An end-to-end agentic SRE system based on the AWS blog post:
https://aws.amazon.com/blogs/devops/building-an-end-to-end-agentic-sre-using-aws-devops-agent/

The system automatically detects incidents on a running Python web app, investigates root causes using AWS DevOps Agent, and posts findings to Slack — with no human intervention needed during an incident.

---

## Key decisions already made

| Decision | Choice | Reason |
|---|---|---|
| Python app type | Flask REST API on EC2 | Richer telemetry (CPU, memory, logs) than Lambda; better for CloudWatch alarms |
| EC2 instance | t3.micro | Cheap, sufficient for a demo |
| Log aggregation | Skip Splunk | Overhead too high for an experiment; GitHub alone gives enough deployment correlation |
| Third-party tools | GitHub + Slack | GitHub for deployment history correlation; Slack for incident notifications |
| AWS accounts | Single account | Simulating the three-account architecture in one account for the experiment |
| Lambda runtime | Node.js 20.x | Matches the HMAC crypto example in the blog |
| IaC | CloudFormation | Provided template.yaml for the Lambda + EventBridge stack |

---

## Architecture (single AWS account)

```
Flask API (EC2 t3.micro)
    │
    │  emits metrics
    ▼
CloudWatch Alarms  ──►  EventBridge Rule  ──►  Lambda (webhook forwarder)
                                                        │
                                                        │  HMAC-signed POST
                                                        ▼
GitHub Repo  ──────────────────────────────►  AWS DevOps Agent Space
(deployment history)                                    │
                                                        │  correlates metrics
                                                        │  + deployments
                                                        │  + root cause
                                                        ▼
                                                  Slack Channel
                                          (root cause + mitigation plan)
```

Data flow:
1. Flask app runs on EC2, emitting CPU / error-rate metrics to CloudWatch
2. CloudWatch Alarm fires → EventBridge rule triggers Lambda
3. Lambda signs payload with HMAC and POSTs to DevOps Agent webhook
4. DevOps Agent queries CloudWatch metrics and GitHub deployment history
5. Agent correlates the deployment timestamp with the error spike
6. Root cause + mitigation plan posted to Slack

---

## Project file structure (to be created)

```
agentic-sre/
├── app/
│   ├── app.py               # Flask application
│   ├── requirements.txt     # Flask, boto3
│   └── appspec.yml          # CodeDeploy deployment spec
├── infra/
│   ├── template.yaml        # CloudFormation: Lambda + EventBridge + IAM
│   └── cloudwatch.yaml      # CloudFormation: EC2 alarms
├── lambda/
│   └── index.mjs            # Webhook forwarder Lambda (already written)
├── skills/
│   └── flask-api-runbook.md # DevOps Agent Skill (investigation runbook)
└── README.md
```

---

## Flask app spec

The app must have:

- `GET /health` — always returns 200, used for health-check alarm baseline
- `GET /items` — normal business endpoint, returns a list
- `POST /items` — write endpoint
- `GET /chaos` — toggleable chaos endpoint; when CHAOS_MODE env var is set to `1`, returns 500 errors and burns CPU. This is how we trigger the CloudWatch alarm for the experiment.

The app should emit a custom CloudWatch metric `FlaskApp/ErrorCount` on every 500 response using boto3, so we have a clean alarm target beyond just EC2 CPU.

---

## CloudWatch alarms to create

| Alarm name | Metric | Threshold | Priority mapping |
|---|---|---|---|
| `flask-api-cpu-high` | EC2 CPUUtilization | > 70% for 2 periods | HIGH |
| `flask-api-errors` | FlaskApp/ErrorCount (custom) | > 10 in 1 minute | HIGH |
| `flask-api-health-check` | StatusCheckFailed | >= 1 | CRITICAL |

---

## Lambda webhook forwarder — already written

File: `lambda/index.mjs`

```javascript
/**
 * Lambda: CloudWatch Alarm → AWS DevOps Agent Webhook
 *
 * Trigger:   EventBridge rule matching CloudWatch alarm state changes
 * Runtime:   Node.js 20.x
 * Env vars:
 *   DEVOPS_AGENT_WEBHOOK_URL   — webhook URL from Agent Space console
 *   DEVOPS_AGENT_WEBHOOK_SECRET — HMAC secret saved when creating the webhook
 */

import { createHmac } from "node:crypto";

const PRIORITY_MAP = {
  CRITICAL: ["cpu-critical", "health-check-failed", "5xx-critical"],
  HIGH:     ["cpu-high",     "error-rate-high",     "latency-p99", "errors"],
  MEDIUM:   ["cpu-medium",   "memory-high",         "disk-usage"],
  LOW:      ["cpu-low",      "request-count"],
};

function derivePriority(alarmName = "") {
  const lower = alarmName.toLowerCase();
  for (const [level, patterns] of Object.entries(PRIORITY_MAP)) {
    if (patterns.some((p) => lower.includes(p))) return level;
  }
  return "MEDIUM";
}

function buildPayload(event) {
  const detail      = event.detail ?? {};
  const alarmName   = detail.alarmName   ?? "unknown-alarm";
  const state       = detail.state       ?? {};
  const alarmArn    = detail.alarmArn    ?? "";
  const accountId   = event.account      ?? "";
  const region      = event.region       ?? "";

  const incidentId = alarmArn || `${accountId}-${alarmName}`;

  const actionMap = {
    ALARM:             "created",
    OK:                "resolved",
    INSUFFICIENT_DATA: "updated",
  };
  const action = actionMap[state.value] ?? "updated";

  return {
    eventType:   "incident",
    incidentId,
    action,
    priority:    derivePriority(alarmName),
    title:       `CloudWatch Alarm: ${alarmName}`,
    description: state.reason ?? `Alarm transitioned to ${state.value}`,
    timestamp:   event.time ?? new Date().toISOString(),
    service:     "ec2-flask-api",
    data: {
      alarmName,
      alarmArn,
      accountId,
      region,
      previousState: detail.previousState?.value,
      currentState:  state.value,
      stateReason:   state.reason,
      rawEvent: event,
    },
  };
}

function sign(payload, secret, timestamp) {
  const hmac = createHmac("sha256", secret);
  hmac.update(`${timestamp}:${JSON.stringify(payload)}`, "utf8");
  return hmac.digest("base64");
}

async function sendToAgent(payload, webhookUrl, secret) {
  const timestamp = new Date().toISOString();
  const signature = sign(payload, secret, timestamp);

  const res = await fetch(webhookUrl, {
    method:  "POST",
    headers: {
      "Content-Type":            "application/json",
      "x-amzn-event-timestamp": timestamp,
      "x-amzn-event-signature": signature,
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`DevOps Agent webhook returned ${res.status}: ${body}`);
  }

  return res.status;
}

export const handler = async (event) => {
  console.log("Received event:", JSON.stringify(event, null, 2));

  const webhookUrl = process.env.DEVOPS_AGENT_WEBHOOK_URL;
  const secret     = process.env.DEVOPS_AGENT_WEBHOOK_SECRET;

  if (!webhookUrl || !secret) {
    throw new Error(
      "Missing env vars: DEVOPS_AGENT_WEBHOOK_URL and/or DEVOPS_AGENT_WEBHOOK_SECRET"
    );
  }

  const records = event.Records ?? [event];

  for (const record of records) {
    const innerEvent =
      record.Sns?.Message
        ? JSON.parse(record.Sns.Message)
        : record;

    const payload = buildPayload(innerEvent);
    console.log("Sending payload:", JSON.stringify(payload, null, 2));

    const status = await sendToAgent(payload, webhookUrl, secret);
    console.log(`DevOps Agent responded with HTTP ${status}`);
  }

  return { statusCode: 200, body: "OK" };
};
```

---

## CloudFormation template — already written

File: `infra/template.yaml`

Deploys:
- IAM role for Lambda (AWSLambdaBasicExecutionRole)
- Lambda function `devops-agent-webhook-forwarder`
- EventBridge rule matching CloudWatch alarm state changes
- Lambda invoke permission for EventBridge

Parameters: `DevOpsAgentWebhookUrl`, `DevOpsAgentWebhookSecret`, `AlarmName`

Deploy command:
```bash
aws cloudformation deploy \
  --template-file infra/template.yaml \
  --stack-name devops-agent-webhook \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      DevOpsAgentWebhookUrl="https://your-webhook-url" \
      DevOpsAgentWebhookSecret="your-secret" \
      AlarmName="flask-api-cpu-high"
```

---

## DevOps Agent Skill to write

File: `skills/flask-api-runbook.md`

This Markdown file is uploaded to the DevOps Agent Operator console under Skills.
It tells the agent how to investigate this specific app.

It should cover:
- App description: Flask REST API on EC2, endpoints /health /items /chaos
- Normal baselines: CPU < 20%, error count = 0, health check always green
- What to check first: CloudWatch CPUUtilization + FlaskApp/ErrorCount custom metric
- Deployment correlation: check GitHub for any push in the 30 minutes before the alarm
- Common root causes for this app: (1) CHAOS_MODE=1 set on EC2, (2) bad deployment, (3) EC2 instance type too small
- Remediation steps: (1) SSH to EC2, unset CHAOS_MODE, (2) roll back via CodePipeline, (3) resize instance

---

## What to build next (in order)

### Step 1 — Flask app
Create `app/app.py` with the four endpoints above.
Create `app/requirements.txt` (flask, boto3).
The `/chaos` endpoint should read the `CHAOS_MODE` environment variable and return 500 + burn CPU when set to `1`. On every 500 response, emit the `FlaskApp/ErrorCount` custom metric to CloudWatch using boto3.

### Step 2 — CodeDeploy spec
Create `app/appspec.yml` so CodePipeline can deploy the Flask app to the EC2 instance.
Include lifecycle hooks: BeforeInstall (install deps), ApplicationStart (start gunicorn), ApplicationStop (stop gunicorn).

### Step 3 — CloudWatch alarms CloudFormation
Create `infra/cloudwatch.yaml` with the three alarms in the table above.
Parameter: `EC2InstanceId`.

### Step 4 — DevOps Agent Skill
Create `skills/flask-api-runbook.md` per the spec above.

### Step 5 — README
Create a `README.md` with end-to-end setup instructions covering:
1. Prerequisites (AWS CLI, Node 20, Python 3.11)
2. GitHub repo setup + CodePipeline connection
3. EC2 launch + CodeDeploy agent install
4. CloudFormation deploy order (cloudwatch.yaml first, then template.yaml)
5. DevOps Agent Space setup steps (webhook, GitHub, CloudWatch, Slack, Skills)
6. How to trigger a test incident (set CHAOS_MODE=1 on EC2)
7. What to expect in Slack + the Operator console

---

## Prompt to paste into Claude Code terminal

Once you open VS Code and start Claude Code (`claude` in terminal), paste the following:

---

```
I am building an end-to-end agentic SRE experiment on AWS using AWS DevOps Agent.
Read the full context in agentic-sre-context.md before doing anything.

The project structure should be:
agentic-sre/
├── app/          (Flask API)
├── infra/        (CloudFormation)
├── lambda/       (webhook forwarder — already written, see context)
├── skills/       (DevOps Agent runbook)
└── README.md

Start with Step 1: create app/app.py and app/requirements.txt.
The Flask app needs four endpoints: /health, /items (GET+POST), and /chaos.
The /chaos endpoint reads CHAOS_MODE env var — when set to 1, return 500 errors
and emit a FlaskApp/ErrorCount custom CloudWatch metric using boto3 on every error.

After app.py is done, move to Step 2 (appspec.yml), then Step 3 (cloudwatch.yaml),
then Step 4 (skills/flask-api-runbook.md), then Step 5 (README.md).

Do not skip any step. Ask me before making AWS API calls.
```

---

## AWS CLI test command (once Lambda is deployed)

Force-trigger the Lambda to test the webhook end-to-end without waiting for a real alarm:

```bash
aws lambda invoke \
  --function-name devops-agent-webhook-forwarder \
  --payload '{"source":"aws.cloudwatch","detail-type":"CloudWatch Alarm State Change","account":"123456789","region":"us-east-1","time":"2026-05-25T10:00:00Z","detail":{"alarmName":"flask-api-errors","alarmArn":"arn:aws:cloudwatch:us-east-1:123456789:alarm:flask-api-errors","state":{"value":"ALARM","reason":"Threshold crossed: 15 errors > 10 threshold"},"previousState":{"value":"OK"}}}' \
  response.json && cat response.json
```

Replace `123456789` with your actual AWS account ID and update the region if needed.

---

## Useful links

- AWS DevOps Agent product page: https://aws.amazon.com/devops-agent/
- Creating an Agent Space: https://docs.aws.amazon.com/devopsagent/latest/userguide/getting-started-with-aws-devops-agent-creating-an-agent-space.html
- Webhook invocation docs: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-capabilities-for-aws-devops-agent-invoking-devops-agent-through-webhook.html
- DevOps Agent Skills: https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-devops-agent-skills.html
- CloudWatch alarm webhook sample: https://github.com/aws-samples/sample-aws-devops-agent-cloudwatch
