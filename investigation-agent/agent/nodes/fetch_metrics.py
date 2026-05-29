"""
fetch_metrics — LangGraph node (parallel)
Fetches CloudWatch metric datapoints for the 30-minute window around the incident.

Metrics fetched in a single get_metric_data call:
  - FlaskApp/ErrorCount      (custom, Sum, 1-min periods)
  - AWS/EC2 CPUUtilization   (Average, 1-min periods)
  - AWS/EC2 NetworkIn        (Average, 1-min periods)
  - AWS/EC2 NetworkOut       (Average, 1-min periods)

EC2 instance ID resolution order:
  1. alarm_details.config.dimensions (populated by fetch_alarm node)
  2. EC2_INSTANCE_ID environment variable
  3. Omit EC2 metrics gracefully if still not found
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from agent.state import InvestigationState

logger = logging.getLogger(__name__)

AWS_REGION      = os.environ.get("AWS_REGION", "eu-north-1")
WINDOW_MINUTES  = 30   # look-back window relative to incident time
PERIOD_SECONDS  = 60   # 1-minute resolution


def _build_cloudwatch_client():
    return boto3.client("cloudwatch", region_name=AWS_REGION)


def _extract_instance_id(state: InvestigationState) -> str | None:
    """
    Try to find the EC2 instance ID from:
      1. alarm_details dimensions (e.g. cpu-high alarm targets an instance)
      2. EC2_INSTANCE_ID env var
    """
    dimensions = (
        state.get("alarm_details", {})
             .get("config", {})
             .get("dimensions", [])
    )
    for dim in dimensions:
        if dim.get("Name") == "InstanceId":
            return dim["Value"]

    return os.environ.get("EC2_INSTANCE_ID")


def _resolve_window(state: InvestigationState) -> tuple[datetime, datetime]:
    """
    Build [start, end] window.
    Prefer the incident timestamp; fall back to now.
    """
    incident = state.get("incident", {})
    ts_str = incident.get("timestamp") or incident.get("time", "")
    try:
        incident_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        incident_time = datetime.now(timezone.utc)

    # Extend ±5 min beyond the window so the spike is never at the edge
    end   = incident_time + timedelta(minutes=5)
    start = incident_time - timedelta(minutes=WINDOW_MINUTES)
    return start, end


def _build_metric_queries(instance_id: str | None) -> list[dict]:
    """
    Construct MetricDataQuery list for get_metric_data.
    EC2 metrics are only added when an instance ID is available.
    """
    queries = [
        {
            "Id":    "error_count",
            "Label": "FlaskApp/ErrorCount",
            "MetricStat": {
                "Metric": {
                    "Namespace":  "FlaskApp",
                    "MetricName": "ErrorCount",
                },
                "Period": PERIOD_SECONDS,
                "Stat":   "Sum",
            },
            "ReturnData": True,
        }
    ]

    if instance_id:
        ec2_dims = [{"Name": "InstanceId", "Value": instance_id}]
        queries += [
            {
                "Id":    "cpu",
                "Label": "AWS/EC2 CPUUtilization",
                "MetricStat": {
                    "Metric": {
                        "Namespace":  "AWS/EC2",
                        "MetricName": "CPUUtilization",
                        "Dimensions": ec2_dims,
                    },
                    "Period": PERIOD_SECONDS,
                    "Stat":   "Average",
                },
                "ReturnData": True,
            },
            {
                "Id":    "net_in",
                "Label": "AWS/EC2 NetworkIn",
                "MetricStat": {
                    "Metric": {
                        "Namespace":  "AWS/EC2",
                        "MetricName": "NetworkIn",
                        "Dimensions": ec2_dims,
                    },
                    "Period": PERIOD_SECONDS,
                    "Stat":   "Average",
                },
                "ReturnData": True,
            },
            {
                "Id":    "net_out",
                "Label": "AWS/EC2 NetworkOut",
                "MetricStat": {
                    "Metric": {
                        "Namespace":  "AWS/EC2",
                        "MetricName": "NetworkOut",
                        "Dimensions": ec2_dims,
                    },
                    "Period": PERIOD_SECONDS,
                    "Stat":   "Average",
                },
                "ReturnData": True,
            },
        ]

    return queries


def _fetch_metrics_sync(
    queries: list[dict],
    start: datetime,
    end: datetime,
) -> list[dict]:
    """
    Blocking boto3 call — called via asyncio.to_thread().
    Returns a flat list of datapoints across all queried metrics.
    """
    cw = _build_cloudwatch_client()
    datapoints: list[dict] = []

    try:
        resp = cw.get_metric_data(
            MetricDataQueries=queries,
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampAscending",
        )

        for result in resp.get("MetricDataResults", []):
            label      = result.get("Label", "")
            timestamps = result.get("Timestamps", [])
            values     = result.get("Values", [])

            for ts, val in zip(timestamps, values):
                datapoints.append({
                    "metric":    label,
                    "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "value":     round(val, 4),
                })

    except ClientError as exc:
        logger.error("[fetch_metrics] get_metric_data failed: %s", exc)
        # Return empty list — synthesize will note the gap

    return datapoints


def _build_observation(
    datapoints: list[dict],
    instance_id: str | None,
    start: datetime,
    end: datetime,
) -> str:
    """
    Summarise the fetched datapoints into a single human-readable observation.
    """
    if not datapoints:
        return (
            "[fetch_metrics] No metric datapoints returned for the investigation window. "
            "ErrorCount and CPU data unavailable."
        )

    # Group by metric name for summary stats
    by_metric: dict[str, list[float]] = {}
    for dp in datapoints:
        by_metric.setdefault(dp["metric"], []).append(dp["value"])

    parts = []
    for metric, values in by_metric.items():
        peak    = max(values)
        avg     = round(sum(values) / len(values), 2)
        nonzero = sum(1 for v in values if v > 0)
        parts.append(
            f"{metric}: peak={peak}, avg={avg}, "
            f"{nonzero}/{len(values)} non-zero 1-min periods"
        )

    window_str = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} UTC"
    instance_str = f" (instance: {instance_id})" if instance_id else " (no instance ID)"

    return (
        f"[fetch_metrics] {window_str}{instance_str} — "
        + "; ".join(parts)
        + f". Total datapoints: {len(datapoints)}."
    )


async def run(state: InvestigationState) -> dict:
    """
    LangGraph async node entry point.
    Runs concurrently with fetch_logs, fetch_cloudtrail, fetch_github.
    """
    instance_id = _extract_instance_id(state)
    start, end  = _resolve_window(state)

    if not instance_id:
        logger.warning(
            "[fetch_metrics] EC2 instance ID not found in alarm dimensions or "
            "EC2_INSTANCE_ID env var — EC2 metrics will be skipped."
        )

    queries = _build_metric_queries(instance_id)

    logger.info(
        "[fetch_metrics] Fetching %d metric series from %s to %s",
        len(queries), start.isoformat(), end.isoformat(),
    )

    datapoints = await asyncio.to_thread(_fetch_metrics_sync, queries, start, end)

    observation = _build_observation(datapoints, instance_id, start, end)
    logger.info("[fetch_metrics] %s", observation)

    # Return only NEW items — operator.add reducer handles concatenation
    gaps = (
        ["EC2 instance ID unavailable — CPUUtilization, NetworkIn/Out not fetched"]
        if not instance_id else []
    )

    return {
        "metrics":            datapoints,
        "observations":       [observation],
        "investigation_gaps": gaps,
    }
