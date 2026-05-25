/**
 * Lambda: CloudWatch Alarm → AWS DevOps Agent Webhook
 *
 * Trigger:   EventBridge rule matching CloudWatch alarm state changes
 * Runtime:   Node.js 20.x
 * Env vars:
 *   DEVOPS_AGENT_WEBHOOK_URL    — webhook URL from Agent Space console
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
