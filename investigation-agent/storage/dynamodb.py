"""
storage/dynamodb.py — DynamoDB investigation history store.

Table schema:
  PK  investigation_id  (String — UUID generated in handler.py)
  SK  created_at        (String — ISO timestamp)

All other fields are plain attributes. Floats are converted to Decimal
because DynamoDB rejects Python floats. Large data sections are capped
to keep items well under the 400 KB DynamoDB limit.
"""

import decimal
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from agent.state import InvestigationState

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "eu-north-1")
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "sre-investigations")

# Item size guards — keep well under 400 KB DynamoDB limit
MAX_STR_CHARS       = 10_000
MAX_METRICS         = 60
MAX_LOGS            = 100
MAX_CLOUDTRAIL      = 50
MAX_OBSERVATIONS    = 20


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_table():
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)


def _to_decimal(obj):
    """Recursively replace float → Decimal (DynamoDB requirement)."""
    if isinstance(obj, float):
        return decimal.Decimal(str(round(obj, 6)))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal(i) for i in obj]
    return obj


def _safe(val: object, max_chars: int = MAX_STR_CHARS) -> str:
    s = str(val) if val is not None else ""
    return s[:max_chars]


def _alarm_name(state: InvestigationState) -> str:
    incident = state.get("incident", {})
    return (
        incident.get("data", {}).get("alarmName")
        or incident.get("detail", {}).get("alarmName")
        or "unknown"
    )


# ── Public API ────────────────────────────────────────────────────────────────

def save_investigation(state: InvestigationState) -> bool:
    """
    Write a new investigation record to DynamoDB.
    Called once after the LangGraph graph completes.
    Returns True on success, False on error (non-fatal).
    """
    now = datetime.now(timezone.utc).isoformat()
    incident = state.get("incident", {})

    item = {
        "investigation_id": state["incident_id"],
        "created_at":        now,
        "incident_id":       str(incident.get("incidentId", state["incident_id"])),
        "alarm_name":        _alarm_name(state),
        "status":            state.get("status", "investigating"),
        "root_cause":        _safe(state.get("root_cause", "")),
        "mitigation_plan":   _safe(state.get("mitigation_plan", "")),
        "observations":      state.get("observations", [])[:MAX_OBSERVATIONS],
        "investigation_gaps": state.get("investigation_gaps", []),
        "slack_thread_ts":   state.get("slack_thread_ts") or "",
        "approved_by":       state.get("approved_by") or "",
        "published_at":      "",
        "llm_provider":      os.environ.get("LLM_PROVIDER", "unknown"),
        "llm_model":         os.environ.get("LLM_MODEL", "unknown"),
        # Raw evidence — capped to stay inside 400 KB
        "raw_data": _to_decimal({
            "alarm_details":     state.get("alarm_details", {}),
            "metrics":           state.get("metrics", [])[:MAX_METRICS],
            "logs":              state.get("logs", [])[:MAX_LOGS],
            "cloudtrail_events": state.get("cloudtrail_events", [])[:MAX_CLOUDTRAIL],
            "github_commits":    state.get("github_commits", []),
            "argocd_data":       state.get("argocd_data", {}),
        }),
    }

    try:
        _get_table().put_item(Item=item)
        logger.info("[dynamodb] Saved investigation %s (alarm: %s)",
                    state["incident_id"], item["alarm_name"])
        return True
    except ClientError as exc:
        logger.error("[dynamodb] save_investigation failed: %s", exc)
        return False


def update_investigation(investigation_id: str, updates: dict) -> bool:
    """
    Patch specific fields on an existing investigation record.
    Typical callers:
      - slack.py sets slack_thread_ts after posting the HITL message
      - slack_handler.py sets status=published, approved_by, published_at on approval
      - slack_handler.py sets status=dismissed on ❌ reaction

    updates: plain dict  e.g. {"status": "published", "approved_by": "U123"}
    """
    table = _get_table()

    # Need the SK (created_at) to address the item — look it up first
    try:
        resp = table.query(
            KeyConditionExpression=Key("investigation_id").eq(investigation_id),
            Limit=1,
        )
        items = resp.get("Items", [])
        if not items:
            logger.warning("[dynamodb] investigation %s not found", investigation_id)
            return False
        created_at = items[0]["created_at"]
    except ClientError as exc:
        logger.error("[dynamodb] lookup for update failed: %s", exc)
        return False

    # Build UpdateExpression dynamically from the updates dict
    set_clauses   = []
    expr_names    = {}
    expr_values   = {}
    for i, (field, value) in enumerate(updates.items()):
        name_key  = f"#f{i}"
        value_key = f":v{i}"
        set_clauses.append(f"{name_key} = {value_key}")
        expr_names[name_key]  = field
        expr_values[value_key] = _to_decimal(value)

    try:
        table.update_item(
            Key={"investigation_id": investigation_id, "created_at": created_at},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
        logger.info("[dynamodb] Updated investigation %s → %s",
                    investigation_id, list(updates.keys()))
        return True
    except ClientError as exc:
        logger.error("[dynamodb] update_investigation failed: %s", exc)
        return False


def get_investigation(investigation_id: str) -> Optional[dict]:
    """
    Retrieve a full investigation record by ID.
    Used by slack_handler.py to fetch context when a reaction arrives.
    """
    try:
        resp = _get_table().query(
            KeyConditionExpression=Key("investigation_id").eq(investigation_id),
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except ClientError as exc:
        logger.error("[dynamodb] get_investigation failed: %s", exc)
        return None
