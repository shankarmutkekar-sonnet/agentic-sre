"""
fetch_logs — LangGraph node (parallel)
Fetches application log entries from two sources concurrently:
  1. CloudWatch Logs  — boto3 filter_log_events on CLOUDWATCH_LOG_GROUP
  2. Splunk Cloud     — REST API search (exec_mode=oneshot)

Both sources are queried for the 30-minute window before the incident.
Results are normalised into a common shape and returned as a single list.

Environment variables:
  CLOUDWATCH_LOG_GROUP  default: /flask-app/logs
  SPLUNK_URL            e.g. https://prd-p-uaboh.splunkcloud.com
  SPLUNK_TOKEN          Splunk HEC / API token
  SPLUNK_INDEX          default: main
  AWS_REGION            default: eu-north-1
"""

import asyncio
import json
import logging
import os
import ssl
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from agent.state import InvestigationState

logger = logging.getLogger(__name__)

AWS_REGION         = os.environ.get("AWS_REGION", "eu-north-1")
CW_LOG_GROUP       = os.environ.get("CLOUDWATCH_LOG_GROUP", "/flask-app/logs")
SPLUNK_URL         = os.environ.get("SPLUNK_URL", "")
SPLUNK_TOKEN       = os.environ.get("SPLUNK_TOKEN", "")
SPLUNK_INDEX       = os.environ.get("SPLUNK_INDEX", "main")
WINDOW_MINUTES     = 30
MAX_CW_EVENTS      = 200   # cap to stay within Lambda memory
MAX_SPLUNK_RESULTS = 200


# ── Shared helpers ────────────────────────────────────────────────────────────

def _resolve_window(state: InvestigationState) -> tuple[datetime, datetime]:
    incident = state.get("incident", {})
    ts_str   = incident.get("timestamp") or incident.get("time", "")
    try:
        incident_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        incident_time = datetime.now(timezone.utc)

    end   = incident_time + timedelta(minutes=5)
    start = incident_time - timedelta(minutes=WINDOW_MINUTES)
    return start, end


# ── CloudWatch Logs ───────────────────────────────────────────────────────────

def _fetch_cwlogs_sync(start: datetime, end: datetime) -> list[dict]:
    """Blocking boto3 call — run via asyncio.to_thread."""
    logs = boto3.client("logs", region_name=AWS_REGION)
    entries: list[dict] = []

    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(end.timestamp() * 1000)

    try:
        paginator = logs.get_paginator("filter_log_events")
        pages = paginator.paginate(
            logGroupName=CW_LOG_GROUP,
            startTime=start_ms,
            endTime=end_ms,
            PaginationConfig={"MaxItems": MAX_CW_EVENTS},
        )
        for page in pages:
            for event in page.get("events", []):
                ts_ms = event.get("timestamp", 0)
                entries.append({
                    "source":    "cloudwatch_logs",
                    "log_group": CW_LOG_GROUP,
                    "stream":    event.get("logStreamName", ""),
                    "timestamp": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(),
                    "message":   event.get("message", "").strip(),
                })
    except ClientError as exc:
        logger.warning("[fetch_logs] CloudWatch Logs error: %s", exc)

    return entries


# ── Splunk ────────────────────────────────────────────────────────────────────

def _fetch_splunk_sync(start: datetime, end: datetime) -> list[dict]:
    """
    Calls Splunk REST API with exec_mode=oneshot (blocking, returns results inline).
    Uses urllib (stdlib) so we don't depend on requests in the Lambda layer.
    SSL verification is disabled for Splunk Cloud self-signed certs.
    """
    if not SPLUNK_URL or not SPLUNK_TOKEN:
        logger.warning("[fetch_logs] Splunk not configured — skipping")
        return []

    # SPL: fetch all events from the Flask app log source in the window
    earliest = start.strftime("%m/%d/%Y:%H:%M:%S")
    latest   = end.strftime("%m/%d/%Y:%H:%M:%S")
    spl = (
        f'search index={SPLUNK_INDEX} source="ec2-flask-app" '
        f'earliest="{earliest}" latest="{latest}" '
        f'| head {MAX_SPLUNK_RESULTS}'
        f'| fields _time, message, level, logger, function, line'
    )

    payload = urllib.parse.urlencode({
        "search":       spl,
        "exec_mode":    "oneshot",
        "output_mode":  "json",
        "count":        MAX_SPLUNK_RESULTS,
    }).encode("utf-8")

    # Splunk Cloud REST API: try the base URL as-is (may include port),
    # then strip port and retry on 443 if first attempt times out.
    base_url = SPLUNK_URL.rstrip("/")
    url = f"{base_url}/services/search/jobs/export"

    # Skip SSL verification for Splunk Cloud self-signed cert
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE

    def _make_request(target_url: str) -> urllib.request.Request:
        return urllib.request.Request(
            target_url,
            data=payload,
            headers={
                "Authorization": f"Splunk {SPLUNK_TOKEN}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
        )

    entries: list[dict] = []
    try:
        with urllib.request.urlopen(_make_request(url), timeout=8, context=ssl_ctx) as resp:
            # The export endpoint streams newline-delimited JSON objects
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Each object is either a "result" or a metadata preview
                result = obj.get("result")
                if not result:
                    continue

                entries.append({
                    "source":    "splunk",
                    "timestamp": result.get("_time", ""),
                    "message":   result.get("message", ""),
                    "level":     result.get("level", ""),
                    "logger":    result.get("logger", ""),
                    "function":  result.get("function", ""),
                    "line":      result.get("line", ""),
                })
    except Exception as exc:
        logger.warning("[fetch_logs] Splunk fetch error on %s: %s", url, exc)
        # Fallback: strip explicit port and retry on default HTTPS (443)
        # Splunk Cloud instances often serve REST API on 443 as well as 8089
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(base_url)
        if parsed.port and parsed.port != 443:
            fallback_netloc = parsed.hostname  # drop port → use 443
            fallback_base   = urlunparse(parsed._replace(netloc=fallback_netloc))
            fallback_url    = f"{fallback_base}/services/search/jobs/export"
            logger.info("[fetch_logs] Retrying Splunk on port 443: %s", fallback_url)
            try:
                with urllib.request.urlopen(
                    _make_request(fallback_url), timeout=8, context=ssl_ctx
                ) as resp:
                    for line in resp:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        result = obj.get("result")
                        if not result:
                            continue
                        entries.append({
                            "source":    "splunk",
                            "timestamp": result.get("_time", ""),
                            "message":   result.get("message", ""),
                            "level":     result.get("level", ""),
                            "logger":    result.get("logger", ""),
                            "function":  result.get("function", ""),
                            "line":      result.get("line", ""),
                        })
            except Exception as exc2:
                logger.warning("[fetch_logs] Splunk fallback (443) also failed: %s", exc2)

    return entries


# ── Node entry point ──────────────────────────────────────────────────────────

async def run(state: InvestigationState) -> dict:
    """
    LangGraph async node — runs concurrently with fetch_metrics, fetch_cloudtrail,
    fetch_github after the fetch_alarm entry node completes.
    """
    start, end = _resolve_window(state)
    logger.info(
        "[fetch_logs] Fetching logs %s → %s",
        start.isoformat(), end.isoformat(),
    )

    # Run CloudWatch Logs and Splunk concurrently
    cw_entries, splunk_entries = await asyncio.gather(
        asyncio.to_thread(_fetch_cwlogs_sync, start, end),
        asyncio.to_thread(_fetch_splunk_sync, start, end),
    )

    all_logs = cw_entries + splunk_entries
    # Sort combined results chronologically
    all_logs.sort(key=lambda e: e.get("timestamp", ""))

    # ── Summarise for observation ─────────────────────────────────────────────
    error_lines = [e for e in all_logs if "error" in e.get("level", "").lower()
                   or "ERROR" in e.get("message", "")]
    gaps: list[str] = []

    if not cw_entries:
        gaps.append(f"CloudWatch Logs group '{CW_LOG_GROUP}' returned no events")
    if not splunk_entries and SPLUNK_URL:
        gaps.append("Splunk returned no events for the investigation window")
    if not SPLUNK_URL:
        gaps.append("Splunk not configured (SPLUNK_URL missing) — logs from Splunk unavailable")

    if all_logs:
        observation = (
            f"[fetch_logs] {len(all_logs)} log entries retrieved "
            f"({len(cw_entries)} CloudWatch, {len(splunk_entries)} Splunk). "
            f"{len(error_lines)} ERROR-level entries in window."
        )
        if error_lines:
            # Surface the first 3 error messages for the LLM
            sample = "; ".join(
                f"{e['timestamp']} — {e['message'][:120]}"
                for e in error_lines[:3]
            )
            observation += f" Sample errors: {sample}"
    else:
        observation = (
            "[fetch_logs] No log entries found in the investigation window. "
            "Log correlation unavailable."
        )

    logger.info("[fetch_logs] %s", observation)

    return {
        "logs":               all_logs,
        "observations":       [observation],
        "investigation_gaps": gaps,
    }
