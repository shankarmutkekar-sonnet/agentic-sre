# When AWS Isn't Enough: How I Built My Own Agentic SRE from Scratch

*Taking inspiration from AWS DevOps Agent and building a fully custom investigation pipeline with LangGraph, Claude, and a Human-in-the-Loop Slack review flow.*

---

## The Pager Goes Off at 2 AM. Now What?

Every SRE knows the feeling. An alert fires, you're jolted awake, and somewhere between half-asleep and full-panic you're tabbing between CloudWatch, your log aggregator, a GitHub diff, and a Slack thread — trying to answer one question: **what broke, and why?**

This is the problem that AWS's new managed service, **AWS DevOps Agent**, was designed to solve. It's an always-on, autonomous investigation engine that watches your infrastructure, correlates telemetry, and wakes you up with a root cause instead of an active incident.

I used it. I liked it. And then I replaced it with something I built myself.

This article is the story of that journey — why I started with AWS DevOps Agent, what it taught me, and how I built a custom agentic SRE pipeline that does the same job while giving me full ownership of the architecture, the prompts, and the LLM.

---

## Part 1: Starting With AWS DevOps Agent — The Benchmark

Before writing a single line of custom code, I integrated AWS DevOps Agent into a real project. The goal was simple: establish a quality benchmark. Understand what "good" autonomous incident investigation actually looks like before trying to replicate it.

### What AWS DevOps Agent Does

At its core, AWS DevOps Agent is an **autonomous frontier agent** that lives in your AWS account (in a construct called an *Agent Space*). You wire it to your observability stack, and it does the rest.

The data flow looks like this:

```
CloudWatch Alarm fires
        ↓
EventBridge → Lambda (webhook forwarder)
        ↓
AWS DevOps Agent Space
        ↓
Agent queries: CloudWatch + Splunk MCP + GitHub
        ↓
Root cause + mitigation plan → Slack
```

What makes it compelling is the **breadth of correlation**. When our demo app fired a high-error-rate alarm, the agent didn't just look at metrics. It cross-referenced:

- The exact CloudWatch alarm state and reason string
- Log patterns in Splunk (via an MCP integration)
- GitHub commit history and PR activity
- CodeDeploy deployment events

In one test, it caught that a CodeDeploy deployment (`d-OQXUNOVLH`) had landed **3 minutes before errors spiked** — and it pinpointed the exact commit hash and a DNS resolution failure (`socket.gaierror: [Errno -2]`) buried in the logs. That's impressive.

### The Setup: Agent Spaces and Webhooks

Configuring an Agent Space involved wiring together several integrations:

**CloudWatch → Agent:** Via an EventBridge rule that triggers a Lambda function, which forwards alarm events to the Agent Space webhook in this format:

```json
{
  "eventType": "incident",
  "incidentId": "string",
  "action": "created",
  "priority": "HIGH",
  "title": "string",
  "description": "optional context",
  "service": "ec2-flask-api"
}
```

**Splunk → Agent:** Via the Splunk MCP Server (available on Splunkbase) and *Better Webhooks* — because Splunk's default webhook doesn't support auth headers.

**GitHub → Agent:** A read-only OAuth app integration that lets the agent correlate deployments with commits.

**Slack → Agent:** A registered workspace integration so the agent can post findings to a designated channel.

The agent also supports **Skills** — Markdown-formatted runbooks that guide its investigation strategy. Think of them as the SRE playbook encoded in plain text.

### What It Got Right

Honestly? Quite a lot. The deployment correlation was excellent. The Splunk MCP integration worked smoothly. The mitigation plans were structured across four phases (Prepare → Pre-Validate → Apply → Post-Validate) and included an *Agent-Ready Spec* — a structured output designed to be fed directly into a coding agent like Kiro.

This last part is genuinely clever: the investigation agent hands off to a coding agent, closing the loop from "alert fired" to "fix is in the codebase."

### The Limitations That Pushed Me to Build Custom

For all its capability, AWS DevOps Agent has a fundamental constraint: **it's a managed black box**.

You can't change the LLM. You can't tune the synthesis prompt. You can't add a custom investigation node that queries your internal CMDB or your internal runbook API. You can't see the investigation logic — you can only configure inputs and read outputs. You pay per investigation, and at scale that adds up.

For teams that want to treat incident investigation as a first-class engineering artifact — something you can version, test, and evolve — a managed service isn't enough.

That's the gap I built for.

---

## Part 2: The Architecture of a Custom Agentic SRE

The goal was to build something functionally equivalent to AWS DevOps Agent, but with full ownership. Here are the design decisions I made.

### Technology Choices

| Concern | AWS DevOps Agent | Custom Agent |
|---|---|---|
| Orchestration | AWS managed | **LangGraph** (stateful graph) |
| LLM | AWS managed (opaque) | **Claude Haiku** via Anthropic API (configurable) |
| Compute | AWS managed | **AWS Lambda** (Python 3.12) |
| Investigation state | Agent Space console | **DynamoDB** |
| Human review | Agent Space UI | **Slack Block Kit (HITL)** |
| Trigger | EventBridge → webhook Lambda → Agent Space | EventBridge → **SQS** → Investigation Lambda |
| Cost model | Per-investigation pricing | LLM API cost only |

**Why LangGraph?** Incident investigation is naturally a parallel, stateful workflow. LangGraph's `StateGraph` lets you model it exactly as it should work: multiple investigation nodes running concurrently, each contributing findings to a shared state, which then converges at an LLM synthesis step.

**Why Claude Haiku?** Fast, cost-effective, and excellent at structured JSON output. The synthesis node prompts Claude to respond with a strict schema: `root_cause`, `mitigation_plan`, `investigation_gaps`. It's also configurable — swapping to GPT-4o or Gemini is a one-line environment variable change.

---

## Part 3: The Investigation Agent — A Deep Dive

### The State Machine

The entire investigation is modelled as an `InvestigationState` TypedDict — a shared data structure that flows through every node:

```python
class InvestigationState(TypedDict):
    # Input
    incident: dict           # Raw SQS payload from EventBridge
    incident_id: str         # UUID (DynamoDB primary key)

    # Populated by parallel nodes
    alarm_details: dict      # CloudWatch alarm config + state history
    metrics: list[dict]      # ErrorCount, CPU datapoints
    logs: list[dict]         # Normalised entries (CloudWatch + Splunk)
    cloudtrail_events: list[dict]
    github_commits: list[dict]  # Commits + merged PRs

    # Synthesis output
    root_cause: str
    mitigation_plan: str
    investigation_gaps: list[str]
    observations: list[str]

    # Metadata
    status: str              # investigating / pending_review / approved / dismissed
    slack_thread_ts: str | None
    approved_by: str | None
```

### The Fan-Out / Fan-In Pattern

This is the architectural heart of the system. LangGraph wires the nodes into a directed graph:

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

All four parallel nodes run concurrently via `asyncio.gather`. The total investigation time is `max(node_times)` — not the sum. In practice, most investigations complete in 15–30 seconds.

### The Five Investigation Nodes

**Node 1 — `fetch_alarm.py`**

Calls `cloudwatch.describe_alarms()` and `describe_alarm_history()`. Extracts the threshold, metric, evaluation period, and reason string. This gives the agent the *what happened* context before anything else runs.

**Node 2 — `fetch_logs.py`**

The most complex node — it queries two log sources simultaneously:

- **CloudWatch Logs**: `filter_log_events()` with pagination (max 200 events), looking back 30 minutes before the incident
- **Splunk REST API**: POST to `/services/search/jobs/export` with a time-bounded SPL query using `exec_mode=oneshot` for synchronous results

A critical infrastructure detail here: **Lambda can't hit Splunk Cloud directly**. Splunk Cloud firewalls port 8089 by IP. Lambda's default outbound IP is dynamic — it changes on every cold start. The fix is placing Lambda in a **VPC with a NAT Gateway** backed by a fixed **Elastic IP**, which gets whitelisted in Splunk Cloud.

```
Private Subnet (Lambda) → NAT Gateway → Elastic IP: 13.63.151.169 → Splunk Cloud
```

**Node 3 — `fetch_metrics.py`**

Calls `cloudwatch.get_metric_data()` for `FlaskApp/ErrorCount` and CPU utilisation. Returns timestamped datapoints so the LLM can see the shape of the spike, not just its peak.

**Node 4 — `fetch_cloudtrail.py`**

Looks back 2 hours for CodeDeploy deployments, SSH sessions (`StartSession`), and IAM changes. This is the *infra change* signal — if CloudTrail is empty, the agent can confidently infer the incident is a code bug, not an infrastructure change.

**Node 5 — `fetch_github.py`**

This node had the most important lesson of the entire project. Initially, it looked back **60 minutes** for commits and **2 hours** for PRs. It missed the root cause of the memory leak scenario entirely.

**Why?** Memory leaks don't fail immediately. A bug deployed at 11:00 might only start throwing errors at 14:00 — three hours later, once the cache fills up. A 60-minute commit window will never see the offending deployment.

The fix: extend to **6 hours for commits** and **24 hours for PRs**. This added a key capability: the agent can now say "the memory leak in `GET /items` was introduced in PR #15, merged 4 hours before this incident."

### Node 6 — The Synthesis Node

All parallel node outputs are formatted into a structured prompt and sent to Claude Haiku. The prompt instructs the model to return strict JSON:

```json
{
  "root_cause": "Unbounded _item_cache in GET /items — each request appends 3,000 items under a timestamp key. After 100 entries per worker, subsequent requests raise MemoryError (HTTP 500).",
  "mitigation_plan": "1. Restart Gunicorn immediately to clear in-memory cache and restore service.\n2. Add LRU eviction policy (maxsize=100) using functools.lru_cache or cachetools.\n3. Add a cache health endpoint for monitoring.\n4. Implement a unit test that verifies cache bounds.",
  "investigation_gaps": [
    "CloudTrail shows no infrastructure changes — confirms this is a code-level bug.",
    "CPU metrics were within normal bounds — ruling out resource exhaustion."
  ]
}
```

The structured output is what makes downstream automation possible. A human reviewer can read it in Slack. An automated system can parse it to create JIRA tickets. A coding agent could theoretically consume the mitigation plan and open a PR.

---

## Part 4: The Human-in-the-Loop Slack Flow

The agent never auto-publishes. Every investigation goes through a human review gate.

After synthesis completes, the agent posts a **Slack Block Kit card** to `#agentic-sre-review`:

```
┌─────────────────────────────────────────┐
│ 🔍 New Investigation — flask-api-errors │
│ ─────────────────────────────────────── │
│ Impact: ErrorCount hit 32 (threshold 10) │
│                                          │
│ Root Cause:                              │
│ Unbounded _item_cache in GET /items...   │
│                                          │
│ Mitigation Plan:                         │
│ 1. Restart Flask to clear cache          │
│ 2. Add LRU eviction policy...            │
│                                          │
│  [✅ Approve]        [❌ Dismiss]        │
└─────────────────────────────────────────┘
```

When a human clicks **Approve**, Slack sends an interactive payload to an API Gateway endpoint, which triggers the `sre-slack-webhook` Lambda. That Lambda:

1. Validates the Slack request signature (HMAC-SHA256)
2. Fetches the full investigation from DynamoDB
3. Posts a detailed incident report to `#agentic-sre-incident`
4. Updates DynamoDB: `status = "approved"`, `approved_by = slack_user_id`

**Dismiss** follows the same path but marks the investigation as dismissed, preserving the full record for retrospectives.

The HITL design reflects a deliberate philosophy: **AI produces, humans decide**. The agent does the heavy lifting of correlation and synthesis. The human adds judgment — context the agent can't have, like "we already know about this" or "this is a known flaky alarm."

---

## Part 5: The Full AWS Infrastructure

All custom agent resources run in **eu-north-1 (Stockholm)**, deployed via a single CloudFormation stack (`agentic-sre-investigation`):

| Resource | Purpose |
|---|---|
| VPC + NAT Gateway | Fixed outbound IP for Splunk connectivity |
| `sre-investigation-agent` Lambda | Core investigation engine (512MB, 900s timeout) |
| `sre-slack-webhook` Lambda | Handles Slack button interactions |
| `sre-investigation-queue` SQS | Decouples alarm trigger from investigation (DLQ after 2 attempts) |
| `sre-investigations` DynamoDB | Persists every investigation for audit and retrospectives |
| API Gateway | Exposes Slack event endpoint |

The Flask app (the simulated production service) runs on EC2 with a CI/CD pipeline through CodePipeline → CodeDeploy, streaming logs to both CloudWatch and Splunk Cloud via HEC.

The alarm trigger chain is:

```
Flask app emits ErrorCount metric
→ CloudWatch Alarm (threshold > 10 over 60s)
→ EventBridge Rule (Input Transformer → incident JSON)
→ SQS Queue
→ Investigation Lambda
```

The SQS buffer is important. It decouples the alarm from the Lambda, preventing alarm storms from hammering the investigation engine simultaneously, and the DLQ catches any investigations that fail after two attempts.

---

## Part 6: Lessons Learned — Custom vs. Managed

Running both systems simultaneously on the same alarms was the best decision I made. Here's the honest comparison:

### Where AWS DevOps Agent Won Initially

On the first test scenario (an inventory service timeout caused by a bad deployment), AWS DevOps Agent found the deployment correlation first. It had better out-of-the-box look-back windows for CodeDeploy events, and its MCP-based Splunk integration was more mature.

The custom agent missed the GitHub correlation entirely — because the 60-minute commit window didn't cover a deployment from 4 hours prior.

**The fix was one configuration change.** That's the point: with a custom agent, you *can* fix it. With a managed service, you wait for AWS.

### The Real Scorecard

| Dimension | AWS DevOps Agent | Custom Agent |
|---|---|---|
| Deployment correlation | Excellent (out of box) | Good (after window fix) |
| Log analysis | Good | Good (CloudWatch + Splunk) |
| Customisability | None | Full |
| Prompt tuning | Not possible | Full control |
| LLM provider | AWS managed (opaque) | Any — Anthropic, OpenAI, Gemini |
| Cost model | Per-investigation pricing | LLM API cost only |
| Investigation history | Agent Space console | DynamoDB (fully queryable) |
| HITL flow | Agent Space UI | Slack (team's primary tool) |
| Audit trail | Limited | Full JSON in DynamoDB |
| Portability | AWS-only | Cloud-agnostic |

### The Hardest Technical Problems

**1. Splunk from Lambda.** Getting a stable, whitelisted IP for Lambda required a full VPC with NAT Gateway. It's not complicated, but it's not obvious either — and it cascades: a Lambda in a VPC private subnet loses direct access to AWS services (DynamoDB, SQS, CloudWatch) unless you add VPC Endpoints or route through the NAT Gateway. We took the NAT approach: simpler, covers everything.

**2. The look-back window problem.** Slow-manifesting bugs (memory leaks, race conditions) break *hours* after the bad code lands. Investigation windows need to be much longer than intuition suggests. The 24-hour PR window exists precisely because someone will merge a leak on Monday afternoon and the alarms won't fire until Tuesday morning.

**3. LLM misconfiguration is silent.** A Lambda that has `LLM_PROVIDER=openai` but a placeholder `OPENAI_API_KEY` will fail silently on synthesis — no Slack card, no error in the investigation flow, just a DynamoDB record stuck at `investigating`. After any CloudFormation stack update, always verify LLM-related environment variables manually.

**4. Gunicorn workers and mixed responses.** During load testing, we got alternating 200s and 500s after triggering the memory leak, which looked like a test bug. It wasn't — it was 2 Gunicorn workers each maintaining independent state. Worker A hits the cache threshold before Worker B. Requests round-robin. The fix for testing: run enough requests to fill both workers' caches (200+ requests for a 2-worker setup).

---

## Part 7: The Investigation in Action

Here's the complete pipeline for Scenario 2 — the memory leak:

**The Bug:**
```python
_item_cache = {}  # Module-level, never cleared

@app.route("/items")
def get_items():
    cache_key = f"items_{time.time()}"
    _item_cache[cache_key] = ["item-1", "item-2", "item-3"] * 1000  # 3000 items/key

    if len(_item_cache) > 100:
        logger.error("CRITICAL: Item cache exceeded safe limit")
        emit_error_metric()
        raise MemoryError(...)
```

**The trigger:** 200+ requests in under 2 minutes via load test script

**What the agent found (30 seconds later):**

- **CloudWatch alarm**: `flask-api-errors` — `ErrorCount` hit 32, threshold is 10
- **Logs (CloudWatch + Splunk)**: `CRITICAL: Item cache exceeded safe limit` appearing repeatedly
- **Metrics**: `ErrorCount` flatlined at 0, then spiked sharply — characteristic unbounded accumulation pattern
- **CloudTrail**: No infrastructure changes — confirms code-level bug
- **GitHub**: PR #15 ("memory leak scenario") merged 4 hours ago to `master`

**Synthesised root cause:**
> "Unbounded `_item_cache` in `GET /items` — each request appends 3,000 items under a unique timestamp key. After 100 entries per Gunicorn worker, all subsequent requests raise `MemoryError` (HTTP 500). The fix was introduced in PR #15, merged at 10:32 UTC."

**Mitigation plan:**
1. Restart Gunicorn immediately to clear in-memory cache and restore service
2. Replace `_item_cache` with `functools.lru_cache(maxsize=100)` or `cachetools.LRUCache`
3. Add cache utilisation metric to prevent recurrence
4. Write a unit test verifying cache bounds

Human approves. Full report posts to `#agentic-sre-incident`. DynamoDB record updated. MTTR: under 5 minutes from alarm fire to approved incident report.

---

## What's Next

The custom agent is running. It matches AWS DevOps Agent on investigation quality (after the look-back window fix) and exceeds it on every dimension that matters for a team that treats reliability as engineering:

- **Scenario 3** — a race condition scenario is next in the roadmap
- **LLM comparison** — running the same incident through Claude Haiku vs GPT-4o to measure output quality
- **Side-by-side benchmark** — a formal evaluation: 10 incidents, both agents, scored on root cause accuracy and mitigation completeness
- **Automated scenario runner** — a script that triggers incidents and captures both agents' outputs for regression testing
- **AWS DevOps Agent disable** — once the benchmark is passed, the managed service goes dark

---

## Final Thoughts

AWS DevOps Agent is a genuinely impressive service, and I recommend starting with it if you want to prove the concept of agentic SRE to your organisation quickly. The managed integration story — especially the Splunk MCP and GitHub correlation — is polished.

But there's a ceiling.

The moment you want to tune the LLM, adjust investigation logic for your specific stack, add a data source AWS doesn't support, or integrate the HITL flow into the tool your team actually uses every day — you've hit that ceiling.

Building custom isn't necessarily harder. The LangGraph fan-out pattern, the DynamoDB state machine, and the Slack HITL flow are each straightforward components. What makes it rewarding is that every improvement you make is permanent, queryable, and yours.

The infrastructure fires at 2 AM regardless. The question is who — or what — is on call with you.

---

*The system described here is built on: LangGraph, Claude Haiku (Anthropic), AWS Lambda (Python 3.12), DynamoDB, SQS, EventBridge, CloudWatch, Splunk Cloud, GitHub REST API v3, and Slack Block Kit. All investigation outputs are stored in DynamoDB with full JSON payloads, enabling retrospectives and quality benchmarking.*
