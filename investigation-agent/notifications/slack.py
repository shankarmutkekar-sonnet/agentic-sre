"""
notifications/slack.py — Slack HITL integration.

Functions:
  post_hitl_review(state)       Post investigation to #sre-review for human approval
  post_to_incidents(state, user) Publish approved report to #incidents
  handle_reaction_event(body)   Process ✅/❌ reaction from Slack Events API webhook

Environment variables:
  SLACK_BOT_TOKEN         xoxb-... bot token
  SLACK_REVIEW_CHANNEL    Channel ID of #agentic-sre-review  (C...)
  SLACK_INCIDENTS_CHANNEL Channel ID of #agentic-sre-incident (C...)
  SLACK_SIGNING_SECRET    Used by slack_handler.py to verify webhook signatures
  DYNAMODB_TABLE          sre-investigations
"""

import logging
import os
from datetime import datetime, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from agent.state import InvestigationState
from storage import dynamodb

logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN         = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_REVIEW_CHANNEL    = os.environ.get("SLACK_REVIEW_CHANNEL", "")
SLACK_INCIDENTS_CHANNEL = os.environ.get("SLACK_INCIDENTS_CHANNEL", "")
DYNAMODB_TABLE          = os.environ.get("DYNAMODB_TABLE", "sre-investigations")


def _client() -> WebClient:
    return WebClient(token=SLACK_BOT_TOKEN)


# ── Message builders ──────────────────────────────────────────────────────────

def _review_blocks(state: InvestigationState) -> list:
    """Build Slack Block Kit message for the HITL review thread."""
    incident    = state.get("incident", {})
    alarm_name  = (
        incident.get("data", {}).get("alarmName")
        or incident.get("title", "Unknown alarm")
    )
    root_cause      = state.get("root_cause", "Analysis unavailable") or "Analysis unavailable"
    mitigation_plan = state.get("mitigation_plan", "No mitigation plan generated") or ""
    gaps            = state.get("investigation_gaps", [])
    inv_id          = state.get("incident_id", "unknown")
    timestamp       = incident.get("timestamp", incident.get("time", "unknown"))

    # Truncate for Slack block text limits (3000 chars max per section)
    root_cause_display  = root_cause[:800]
    mitigation_display  = mitigation_plan[:800]

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🔍 New Investigation — {alarm_name}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Alarm*\n{alarm_name}"},
                {"type": "mrkdwn", "text": f"*Triggered*\n{timestamp}"},
                {"type": "mrkdwn", "text": f"*Priority*\n{incident.get('priority', 'UNKNOWN')}"},
                {"type": "mrkdwn", "text": f"*Investigation ID*\n`{inv_id}`"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Root Cause*\n{root_cause_display}",
            },
        },
    ]

    if mitigation_display:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Mitigation Plan*\n{mitigation_display}",
            },
        })

    if gaps:
        gaps_text = "\n".join(f"• {g}" for g in gaps[:5])
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Investigation Gaps*\n{gaps_text}",
            },
        })

    blocks += [
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve & Publish"},
                    "style": "primary",
                    "action_id": "approve_investigation",
                    "value": inv_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔄 Rollback via ArgoCD"},
                    "action_id": "rollback_via_argocd",
                    "value": inv_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Dismiss"},
                    "style": "danger",
                    "action_id": "dismiss_investigation",
                    "value": inv_id,
                },
            ],
        },
    ]

    return blocks


def _incidents_blocks(state: InvestigationState, approved_by_user: str) -> list:
    """Build the published incident report for #incidents."""
    incident   = state.get("incident", {})
    alarm_name = (
        incident.get("data", {}).get("alarmName")
        or incident.get("title", "Unknown alarm")
    )
    root_cause      = state.get("root_cause", "") or ""
    mitigation_plan = state.get("mitigation_plan", "") or ""
    gaps            = state.get("investigation_gaps", [])
    inv_id          = state.get("incident_id", "unknown")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"✅ Incident Report — {alarm_name}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Approved by* <@{approved_by_user}>",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Root Cause*\n{root_cause[:1200]}",
            },
        },
    ]

    if mitigation_plan:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Mitigation Plan*\n{mitigation_plan[:1200]}",
            },
        })

    if gaps:
        gaps_text = "\n".join(f"• {g}" for g in gaps[:5])
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Investigation Gaps*\n{gaps_text}",
            },
        })

    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"Investigation ID: `{inv_id}` | Table: `{DYNAMODB_TABLE}`",
            }
        ],
    })

    return blocks


# ── Public functions ──────────────────────────────────────────────────────────

def post_hitl_review(state: InvestigationState) -> str | None:
    """
    Post the investigation to #sre-review for human approval.
    Returns the Slack message timestamp (ts) on success, None on failure.
    The ts is stored in DynamoDB as slack_thread_ts for reaction matching.
    """
    if not SLACK_BOT_TOKEN or not SLACK_REVIEW_CHANNEL:
        logger.warning("[slack] SLACK_BOT_TOKEN or SLACK_REVIEW_CHANNEL not set — skipping HITL post")
        return None

    client = _client()
    alarm_name = (
        state.get("incident", {}).get("data", {}).get("alarmName", "Unknown alarm")
    )

    try:
        resp = client.chat_postMessage(
            channel=SLACK_REVIEW_CHANNEL,
            text=f"🔍 New investigation: {alarm_name} — react ✅ to approve",
            blocks=_review_blocks(state),
        )
        ts = resp["ts"]
        logger.info("[slack] HITL review posted to %s, ts=%s", SLACK_REVIEW_CHANNEL, ts)
        return ts
    except SlackApiError as exc:
        logger.error("[slack] Failed to post HITL review: %s", exc.response["error"])
        return None


def post_to_incidents(state: InvestigationState, approved_by_user: str) -> bool:
    """
    Publish the approved investigation report to #incidents.
    Called from slack_handler.py when a ✅ reaction is received.
    Returns True on success.
    """
    if not SLACK_BOT_TOKEN or not SLACK_INCIDENTS_CHANNEL:
        logger.warning("[slack] SLACK_INCIDENTS_CHANNEL not set — cannot publish")
        return False

    client = _client()
    alarm_name = (
        state.get("incident", {}).get("data", {}).get("alarmName", "Unknown alarm")
    )

    try:
        client.chat_postMessage(
            channel=SLACK_INCIDENTS_CHANNEL,
            text=f"✅ Incident resolved: {alarm_name} (approved by <@{approved_by_user}>)",
            blocks=_incidents_blocks(state, approved_by_user),
        )
        logger.info("[slack] Published to %s (approved by %s)", SLACK_INCIDENTS_CHANNEL, approved_by_user)
        return True
    except SlackApiError as exc:
        logger.error("[slack] Failed to publish to incidents: %s", exc.response["error"])
        return False


def handle_reaction_event(body: dict) -> dict:
    """
    Process an incoming Slack reaction_added event from the Events API.
    Called by slack_handler.py after signature verification.

    Flow:
      ✅ white_check_mark → fetch investigation from DynamoDB by slack_thread_ts
                          → publish to #incidents
                          → update DynamoDB: status=published, approved_by, published_at
      ❌ x               → update DynamoDB: status=dismissed

    Returns a response dict for the Lambda to return to Slack.
    """
    event = body.get("event", {})
    reaction    = event.get("reaction", "")
    user_id     = event.get("user", "")
    item        = event.get("item", {})
    channel     = item.get("channel", "")
    message_ts  = item.get("ts", "")

    logger.info("[slack] reaction_added: reaction=%s user=%s ts=%s", reaction, user_id, message_ts)

    # Only handle reactions on the review channel
    if channel != SLACK_REVIEW_CHANNEL:
        return {"statusCode": 200, "body": "ignored — wrong channel"}

    if reaction not in ("white_check_mark", "x"):
        return {"statusCode": 200, "body": "ignored — unrecognised reaction"}

    # Find the investigation by slack_thread_ts
    table = dynamodb._get_table()
    try:
        resp = table.scan(
            FilterExpression="slack_thread_ts = :ts",
            ExpressionAttributeValues={":ts": message_ts},
            Limit=1,
        )
        items = resp.get("Items", [])
    except Exception as exc:
        logger.error("[slack] DynamoDB scan failed: %s", exc)
        return {"statusCode": 500, "body": "DynamoDB error"}

    if not items:
        logger.warning("[slack] No investigation found for ts=%s", message_ts)
        return {"statusCode": 200, "body": "no matching investigation"}

    record = items[0]
    inv_id = record["investigation_id"]

    # Already actioned — ignore duplicate reactions
    if record.get("status") in ("published", "dismissed"):
        logger.info("[slack] Investigation %s already %s — ignoring", inv_id, record["status"])
        return {"statusCode": 200, "body": "already actioned"}

    if reaction == "white_check_mark":
        # Reconstruct minimal state for the incidents post
        state_for_post: InvestigationState = {
            "incident_id":       inv_id,
            "incident":          {},
            "root_cause":        record.get("root_cause", ""),
            "mitigation_plan":   record.get("mitigation_plan", ""),
            "investigation_gaps": record.get("investigation_gaps", []),
            "alarm_details":     {},
            "metrics":           [],
            "logs":              [],
            "cloudtrail_events": [],
            "github_commits":    [],
            "observations":      [],
            "status":            "published",
            "approved_by":       user_id,
            "slack_thread_ts":   message_ts,
        }
        # Restore alarm name into incident dict for block builder
        state_for_post["incident"] = {
            "data": {"alarmName": record.get("alarm_name", "Unknown alarm")},
            "title": record.get("alarm_name", ""),
        }

        published = post_to_incidents(state_for_post, user_id)
        if published:
            dynamodb.update_investigation(inv_id, {
                "status":       "published",
                "approved_by":  user_id,
                "published_at": datetime.now(timezone.utc).isoformat(),
            })
            logger.info("[slack] Investigation %s published by %s", inv_id, user_id)
            return {"statusCode": 200, "body": "published"}
        else:
            return {"statusCode": 500, "body": "failed to publish"}

    else:  # reaction == "x"
        dynamodb.update_investigation(inv_id, {"status": "dismissed", "approved_by": user_id})
        logger.info("[slack] Investigation %s dismissed by %s", inv_id, user_id)
        return {"statusCode": 200, "body": "dismissed"}
