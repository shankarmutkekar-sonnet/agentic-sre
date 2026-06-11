"""
slack_handler.py — Lambda entry point for the Slack Events API webhook.

This Lambda is triggered by API Gateway (POST /slack/events).
It handles three request types:

  1. url_verification  — Slack sends a one-time challenge when you first
                         configure the Events API / Interactivity endpoint.

  2. block_actions     — Button clicks (Approve / Dismiss) from the HITL
                         review card. Sent by Slack Interactivity feature.
                         Payload is URL-encoded form data.

  3. event_callback    — Reserved for future event subscriptions.

Signature verification:
  Every incoming request is verified against SLACK_SIGNING_SECRET.
  On failure we return 200 (not 403) so Slack does not disable delivery.

Environment variables:
  SLACK_SIGNING_SECRET   From the Slack app's Basic Information page
  SLACK_BOT_TOKEN        Used to post the published report
  SLACK_INCIDENTS_CHANNEL
  DYNAMODB_TABLE         sre-investigations
  AWS_REGION             eu-north-1
"""

import hashlib
import hmac
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from notifications.slack import post_to_incidents
from storage import dynamodb as db

ARGOCD_URL      = os.environ.get("ARGOCD_URL", "")
ARGOCD_TOKEN    = os.environ.get("ARGOCD_TOKEN", "")
ARGOCD_APP_NAME = os.environ.get("ARGOCD_APP_NAME", "flask-app")
_VERIFY_SSL     = os.environ.get("ARGOCD_VERIFY_SSL", "false").lower() == "true"


def _argocd_ssl_context():
    ctx = ssl.create_default_context()
    if not _VERIFY_SSL:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _trigger_argocd_rollback(revision: str | None = None) -> tuple[bool, str]:
    """
    Call the ArgoCD API to roll the application back to the previous revision.
    If revision is None, ArgoCD rolls back to the most recent successful sync.
    Returns (success, message).
    """
    if not ARGOCD_URL or not ARGOCD_TOKEN:
        return False, "ArgoCD not configured (ARGOCD_URL/ARGOCD_TOKEN missing)"

    url  = f"{ARGOCD_URL.rstrip('/')}/api/v1/applications/{ARGOCD_APP_NAME}/rollback"
    body = json.dumps({"revision": revision} if revision else {}).encode()
    req  = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization":  f"Bearer {ARGOCD_TOKEN}",
            "Content-Type":   "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_argocd_ssl_context()) as resp:
            result = json.loads(resp.read().decode())
            op_phase = (
                result.get("status", {})
                      .get("operationState", {})
                      .get("phase", "initiated")
            )
            return True, f"Rollback {op_phase}"
    except urllib.error.HTTPError as exc:
        return False, f"ArgoCD API HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, f"ArgoCD rollback error: {exc}"

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")

# Replay-attack window — reject requests older than 5 minutes
MAX_AGE_SECONDS = 300


# ── Signature verification ─────────────────────────────────────────────────────

def _verify_slack_signature(headers: dict, raw_body: str) -> bool:
    """
    Verify the Slack request signature (v0 scheme).
    Returns True if valid, False otherwise.
    """
    if not SLACK_SIGNING_SECRET:
        logger.warning("[slack_handler] SLACK_SIGNING_SECRET not set — skipping verification")
        return True

    lower_headers = {k.lower(): v for k, v in headers.items()}
    timestamp  = lower_headers.get("x-slack-request-timestamp", "")
    slack_sig  = lower_headers.get("x-slack-signature", "")

    if not timestamp or not slack_sig:
        logger.warning("[slack_handler] Missing Slack signature headers")
        return False

    try:
        ts_int = int(timestamp)
    except ValueError:
        return False

    if abs(time.time() - ts_int) > MAX_AGE_SECONDS:
        logger.warning("[slack_handler] Request timestamp too old — possible replay attack")
        return False

    sig_basestring = f"v0:{timestamp}:{raw_body}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, slack_sig):
        logger.warning("[slack_handler] Slack signature mismatch")
        return False

    return True


# ── Button click handler ───────────────────────────────────────────────────────

def _handle_block_action(payload: dict) -> dict:
    """
    Handle Approve / Dismiss button clicks from the HITL review card.

    payload shape (Slack block_actions):
      {
        "type":    "block_actions",
        "user":    {"id": "U...", ...},
        "actions": [{"action_id": "approve_investigation", "value": "<inv_id>"}]
      }
    """
    actions = payload.get("actions", [])
    if not actions:
        return {"statusCode": 200, "body": json.dumps({"status": "no_actions"})}

    action    = actions[0]
    action_id = action.get("action_id", "")
    inv_id    = action.get("value", "")
    user_id   = payload.get("user", {}).get("id", "")

    logger.info("[slack_handler] block_action: action_id=%s inv_id=%s user=%s",
                action_id, inv_id, user_id)

    if not inv_id:
        logger.warning("[slack_handler] No investigation_id in action value")
        return {"statusCode": 200, "body": json.dumps({"status": "missing_inv_id"})}

    # Fetch investigation record from DynamoDB
    record = db.get_investigation(inv_id)
    if not record:
        logger.warning("[slack_handler] Investigation %s not found in DynamoDB", inv_id)
        return {"statusCode": 200, "body": json.dumps({"status": "not_found"})}

    # Guard: already actioned
    if record.get("status") in ("published", "dismissed"):
        logger.info("[slack_handler] Investigation %s already %s — ignoring",
                    inv_id, record["status"])
        return {"statusCode": 200, "body": json.dumps({"status": "already_actioned"})}

    if action_id == "approve_investigation":
        # Reconstruct minimal state for post_to_incidents
        state = {
            "incident_id":        inv_id,
            "incident":           {
                "data": {"alarmName": record.get("alarm_name", "Unknown alarm")},
                "title": record.get("alarm_name", ""),
            },
            "root_cause":         record.get("root_cause", ""),
            "mitigation_plan":    record.get("mitigation_plan", ""),
            "investigation_gaps": record.get("investigation_gaps", []),
            "alarm_details":      {},
            "metrics":            [],
            "logs":               [],
            "cloudtrail_events":  [],
            "github_commits":     [],
            "observations":       [],
            "status":             "published",
            "approved_by":        user_id,
            "slack_thread_ts":    record.get("slack_thread_ts", ""),
        }
        published = post_to_incidents(state, user_id)
        if published:
            db.update_investigation(inv_id, {
                "status":       "published",
                "approved_by":  user_id,
                "published_at": datetime.now(timezone.utc).isoformat(),
            })
            logger.info("[slack_handler] Investigation %s published by %s", inv_id, user_id)
            return {"statusCode": 200, "body": json.dumps({"status": "published"})}
        else:
            return {"statusCode": 200, "body": json.dumps({"status": "publish_failed"})}

    elif action_id == "rollback_via_argocd":
        argocd_data = record.get("raw_data", {}).get("argocd_data", {})
        prev_revision = None
        history = argocd_data.get("sync_history", [])
        if len(history) >= 2:
            prev_revision = history[-2].get("revision")

        success, msg = _trigger_argocd_rollback(prev_revision)
        db.update_investigation(inv_id, {
            "argocd_rollback_requested_by": user_id,
            "argocd_rollback_result":       msg,
            "argocd_rollback_at":           datetime.now(timezone.utc).isoformat(),
        })
        status = "rollback_triggered" if success else "rollback_failed"
        logger.info("[slack_handler] ArgoCD rollback for %s by %s: %s", inv_id, user_id, msg)
        return {"statusCode": 200, "body": json.dumps({"status": status, "message": msg})}

    elif action_id == "dismiss_investigation":
        db.update_investigation(inv_id, {
            "status":      "dismissed",
            "approved_by": user_id,
        })
        logger.info("[slack_handler] Investigation %s dismissed by %s", inv_id, user_id)
        return {"statusCode": 200, "body": json.dumps({"status": "dismissed"})}

    logger.warning("[slack_handler] Unknown action_id: %s", action_id)
    return {"statusCode": 200, "body": json.dumps({"status": "unknown_action"})}


# ── Lambda handler ─────────────────────────────────────────────────────────────

def handler(event: dict, context: Any) -> dict:
    """
    API Gateway Lambda proxy handler.
    Handles both Slack Events API (JSON body) and
    Slack Interactivity (URL-encoded body with payload= field).
    """
    headers  = event.get("headers") or {}
    if not headers:
        multi   = event.get("multiValueHeaders") or {}
        headers = {k: v[0] for k, v in multi.items() if v}

    raw_body = event.get("body") or ""

    logger.info("[slack_handler] Header keys received: %s", list(headers.keys()))

    # ── Signature verification ────────────────────────────────────────────────
    if not _verify_slack_signature(headers, raw_body):
        logger.warning("[slack_handler] Signature verification failed — dropping event")
        return {"statusCode": 200, "body": json.dumps({"status": "rejected"})}

    # ── Interactive component (button click) ──────────────────────────────────
    # Slack sends interactivity payloads as URL-encoded form: payload=<JSON>
    if raw_body.startswith("payload="):
        try:
            payload_json = urllib.parse.unquote_plus(raw_body[len("payload="):])
            payload      = json.loads(payload_json)
        except Exception as exc:
            logger.error("[slack_handler] Failed to parse interactive payload: %s", exc)
            return {"statusCode": 200, "body": json.dumps({"status": "parse_error"})}

        payload_type = payload.get("type", "")
        logger.info("[slack_handler] Interactive payload type: %s", payload_type)

        if payload_type == "block_actions":
            result = _handle_block_action(payload)
            return result

        return {"statusCode": 200, "body": json.dumps({"status": "ignored"})}

    # ── JSON body (Events API) ────────────────────────────────────────────────
    try:
        body = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        logger.error("[slack_handler] Failed to parse JSON body")
        return {"statusCode": 200, "body": json.dumps({"status": "parse_error"})}

    event_type = body.get("type", "")
    logger.info("[slack_handler] Received event type: %s", event_type)

    # URL verification challenge
    if event_type == "url_verification":
        challenge = body.get("challenge", "")
        logger.info("[slack_handler] URL verification challenge received")
        return {
            "statusCode": 200,
            "headers":    {"Content-Type": "application/json"},
            "body":       json.dumps({"challenge": challenge}),
        }

    # Other event callbacks (reserved for future use)
    if event_type == "event_callback":
        logger.info("[slack_handler] event_callback received — no handler configured")
        return {"statusCode": 200, "body": json.dumps({"status": "ignored"})}

    logger.info("[slack_handler] Unknown event type: %s", event_type)
    return {"statusCode": 200, "body": json.dumps({"status": "ignored"})}
