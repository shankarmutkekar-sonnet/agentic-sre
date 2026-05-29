"""
fetch_alarm — LangGraph node
Fetches CloudWatch alarm configuration and recent state-change history.

This is the ENTRY node. It runs first and populates alarm_details before
the four parallel investigation nodes fan out.

CloudWatch calls (all sync boto3, run in thread so the event loop stays free):
  - describe_alarms         → alarm config, thresholds, dimensions
  - describe_alarm_history  → last 10 state transitions (ALARM/OK/INSUFFICIENT_DATA)
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from agent.state import InvestigationState

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "eu-north-1")


def _build_cloudwatch_client():
    return boto3.client("cloudwatch", region_name=AWS_REGION)


def _fetch_alarm_sync(alarm_name: str) -> dict:
    """
    Blocking boto3 calls — wrapped in asyncio.to_thread() by the async runner.
    Returns a dict with keys: config, history, alarm_name, fetch_error.
    """
    cw = _build_cloudwatch_client()
    result = {
        "alarm_name": alarm_name,
        "config": {},
        "history": [],
        "fetch_error": None,
    }

    # ── 1. Alarm configuration ────────────────────────────────────────────────
    try:
        resp = cw.describe_alarms(AlarmNames=[alarm_name], AlarmTypes=["MetricAlarm"])
        alarms = resp.get("MetricAlarms", [])
        if alarms:
            alarm = alarms[0]
            result["config"] = {
                "alarm_name":           alarm.get("AlarmName"),
                "alarm_arn":            alarm.get("AlarmArn"),
                "alarm_description":    alarm.get("AlarmDescription", ""),
                "state_value":          alarm.get("StateValue"),
                "state_reason":         alarm.get("StateReason", ""),
                "state_updated_at":     alarm.get("StateUpdatedTimestamp", "").isoformat()
                                        if hasattr(alarm.get("StateUpdatedTimestamp", ""), "isoformat")
                                        else str(alarm.get("StateUpdatedTimestamp", "")),
                "metric_name":          alarm.get("MetricName"),
                "namespace":            alarm.get("Namespace"),
                "statistic":            alarm.get("Statistic"),
                "period_seconds":       alarm.get("Period"),
                "evaluation_periods":   alarm.get("EvaluationPeriods"),
                "threshold":            alarm.get("Threshold"),
                "comparison_operator":  alarm.get("ComparisonOperator"),
                "treat_missing_data":   alarm.get("TreatMissingData"),
                "dimensions":           alarm.get("Dimensions", []),
                "actions_enabled":      alarm.get("ActionsEnabled", False),
            }
        else:
            result["fetch_error"] = f"Alarm '{alarm_name}' not found in CloudWatch"
            logger.warning(result["fetch_error"])
    except ClientError as exc:
        result["fetch_error"] = f"describe_alarms failed: {exc}"
        logger.error(result["fetch_error"])

    # ── 2. Alarm state-change history (last 2 hours) ──────────────────────────
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=2)

        paginator = cw.get_paginator("describe_alarm_history")
        pages = paginator.paginate(
            AlarmName=alarm_name,
            HistoryItemType="StateUpdate",
            StartDate=start,
            EndDate=now,
            PaginationConfig={"MaxItems": 10},
        )
        history = []
        for page in pages:
            for item in page.get("AlarmHistoryItems", []):
                ts = item.get("Timestamp")
                history.append({
                    "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "history_item_type": item.get("HistoryItemType"),
                    "history_summary":   item.get("HistorySummary", ""),
                    "history_data":      item.get("HistoryData", ""),
                })
        result["history"] = history
    except ClientError as exc:
        logger.warning("describe_alarm_history failed: %s", exc)
        # Non-fatal — config is more important than history

    return result


def _extract_alarm_name(state: InvestigationState) -> str:
    """
    Pull the alarm name out of the incident payload.
    Handles both the webhook-forwarder payload shape and a raw EventBridge event.
    """
    incident = state.get("incident", {})

    # Webhook forwarder shape: incident["data"]["alarmName"]
    alarm_name = incident.get("data", {}).get("alarmName", "")

    # Raw EventBridge shape fallback: incident["detail"]["alarmName"]
    if not alarm_name:
        alarm_name = incident.get("detail", {}).get("alarmName", "")

    # Title fallback: "CloudWatch Alarm: flask-api-errors"
    if not alarm_name:
        title = incident.get("title", "")
        if ":" in title:
            alarm_name = title.split(":", 1)[1].strip()

    return alarm_name or "unknown-alarm"


async def run(state: InvestigationState) -> dict:
    """
    LangGraph async node entry point.
    Returns a partial state dict — LangGraph merges it into the shared state.
    """
    alarm_name = _extract_alarm_name(state)
    logger.info("[fetch_alarm] Fetching details for alarm: %s", alarm_name)

    # Run blocking boto3 calls off the event loop thread
    alarm_details = await asyncio.to_thread(_fetch_alarm_sync, alarm_name)

    # Build the observation string for the synthesize node
    config = alarm_details.get("config", {})
    history = alarm_details.get("history", [])
    fetch_error = alarm_details.get("fetch_error")

    if fetch_error:
        observation = (
            f"[fetch_alarm] ERROR: {fetch_error}. "
            "Alarm configuration unavailable — proceeding with metric data only."
        )
    else:
        observation = (
            f"[fetch_alarm] Alarm '{alarm_name}' is in state {config.get('state_value')}. "
            f"Threshold: {config.get('metric_name')} {config.get('comparison_operator')} "
            f"{config.get('threshold')} over {config.get('evaluation_periods')} × "
            f"{config.get('period_seconds')}s periods. "
            f"Reason: {config.get('state_reason', 'N/A')}. "
            f"State history: {len(history)} transition(s) in the last 2 hours."
        )

    logger.info("[fetch_alarm] %s", observation)

    # Return only the NEW observation — the operator.add reducer in
    # InvestigationState concatenates it with observations from other nodes.
    return {
        "alarm_details": alarm_details,
        "observations":  [observation],
        "status":        "investigating",
    }
