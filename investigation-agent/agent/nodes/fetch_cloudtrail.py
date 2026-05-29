"""
fetch_cloudtrail — LangGraph node (parallel)
Fetches CloudTrail events for the 2-hour window around the incident.

Focus event sources (most relevant for Flask-on-EC2 incidents):
  - codedeploy.amazonaws.com     → deployments
  - ec2.amazonaws.com            → instance changes, SSH sessions via SSM
  - iam.amazonaws.com            → privilege escalation, key changes
  - cloudwatch.amazonaws.com     → alarm acknowledgements, threshold changes
  - ssm.amazonaws.com            → SSM sessions (proxy for SSH)
  - s3.amazonaws.com             → artifact uploads (CodeDeploy revisions)

CloudTrail lookup_events is paginated; we cap at MAX_EVENTS to stay within
Lambda memory and runtime budgets.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from agent.state import InvestigationState

logger = logging.getLogger(__name__)

AWS_REGION  = os.environ.get("AWS_REGION", "eu-north-1")
WINDOW_HOURS = 2
MAX_EVENTS   = 100

RELEVANT_EVENT_SOURCES = {
    "codedeploy.amazonaws.com",
    "ec2.amazonaws.com",
    "iam.amazonaws.com",
    "cloudwatch.amazonaws.com",
    "ssm.amazonaws.com",
    "s3.amazonaws.com",
}

# High-signal event names — always surface these in the observation
HIGH_SIGNAL_EVENTS = {
    "CreateDeployment",
    "StopDeployment",
    "StartSession",         # SSM (proxy for SSH)
    "TerminateSession",
    "RunInstances",
    "StopInstances",
    "TerminateInstances",
    "PutMetricAlarm",
    "DeleteAlarm",
    "AssumeRole",
    "CreateAccessKey",
    "AttachUserPolicy",
    "PutUserPolicy",
}


def _resolve_window(state: InvestigationState) -> tuple[datetime, datetime]:
    incident = state.get("incident", {})
    ts_str   = incident.get("timestamp") or incident.get("time", "")
    try:
        incident_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        incident_time = datetime.now(timezone.utc)

    # 2-hour look-back + 10-min forward buffer
    end   = incident_time + timedelta(minutes=10)
    start = incident_time - timedelta(hours=WINDOW_HOURS)
    return start, end


def _fetch_cloudtrail_sync(start: datetime, end: datetime) -> list[dict]:
    """
    Blocking boto3 call — run via asyncio.to_thread.
    Uses lookup_events (no trail setup needed) with a time-based filter.
    """
    ct = boto3.client("cloudtrail", region_name=AWS_REGION)
    events: list[dict] = []

    try:
        paginator = ct.get_paginator("lookup_events")
        pages = paginator.paginate(
            StartTime=start,
            EndTime=end,
            PaginationConfig={"MaxItems": MAX_EVENTS},
        )

        for page in pages:
            for event in page.get("Events", []):
                event_source = event.get("EventSource", "")

                # Filter to relevant event sources only
                if event_source not in RELEVANT_EVENT_SOURCES:
                    continue

                event_time = event.get("EventTime")
                resources  = [
                    {"type": r.get("ResourceType", ""), "name": r.get("ResourceName", "")}
                    for r in event.get("Resources") or []
                ]

                # Parse CloudTrailEvent JSON for extra detail
                raw_json   = event.get("CloudTrailEvent", "{}")
                try:
                    raw_detail = __import__("json").loads(raw_json)
                except Exception:
                    raw_detail = {}

                events.append({
                    "event_id":      event.get("EventId", ""),
                    "event_name":    event.get("EventName", ""),
                    "event_source":  event_source,
                    "event_time":    event_time.isoformat() if hasattr(event_time, "isoformat") else str(event_time),
                    "username":      event.get("Username", ""),
                    "resources":     resources,
                    "source_ip":     raw_detail.get("sourceIPAddress", ""),
                    "user_agent":    raw_detail.get("userAgent", ""),
                    "request_params": raw_detail.get("requestParameters", {}),
                    "error_code":    raw_detail.get("errorCode", ""),
                    "error_message": raw_detail.get("errorMessage", ""),
                })

    except ClientError as exc:
        logger.error("[fetch_cloudtrail] lookup_events failed: %s", exc)

    # Sort chronologically
    events.sort(key=lambda e: e.get("event_time", ""))
    return events


async def run(state: InvestigationState) -> dict:
    """LangGraph async node — runs in parallel with other investigation nodes."""
    start, end = _resolve_window(state)
    logger.info(
        "[fetch_cloudtrail] Fetching CloudTrail events %s → %s",
        start.isoformat(), end.isoformat(),
    )

    ct_events = await asyncio.to_thread(_fetch_cloudtrail_sync, start, end)

    # ── Summarise for observation ─────────────────────────────────────────────
    gaps: list[str] = []

    if not ct_events:
        observation = (
            "[fetch_cloudtrail] No relevant CloudTrail events found in the "
            f"{WINDOW_HOURS}-hour window. No deployments, SSH sessions, or "
            "IAM changes detected."
        )
        gaps.append(
            "CloudTrail returned no relevant events — possible trail not enabled "
            "or events outside the query window"
        )
    else:
        # Count by event name
        by_name: dict[str, int] = {}
        for e in ct_events:
            by_name[e["event_name"]] = by_name.get(e["event_name"], 0) + 1

        high_signal = [
            e for e in ct_events if e["event_name"] in HIGH_SIGNAL_EVENTS
        ]

        summary_parts = [f"{name}×{count}" for name, count in by_name.items()]
        observation = (
            f"[fetch_cloudtrail] {len(ct_events)} events from "
            f"{len(set(e['event_source'] for e in ct_events))} sources. "
            f"Events: {', '.join(summary_parts)}."
        )
        if high_signal:
            hs_summary = "; ".join(
                f"{e['event_name']} by {e['username']} at {e['event_time']}"
                for e in high_signal[:5]
            )
            observation += f" HIGH-SIGNAL: {hs_summary}."

    logger.info("[fetch_cloudtrail] %s", observation)

    return {
        "cloudtrail_events":  ct_events,
        "observations":       [observation],
        "investigation_gaps": gaps,
    }
