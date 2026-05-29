"""
handler.py — AWS Lambda entry point for the SRE Investigation Agent.

Trigger: SQS queue (buffered from the existing EventBridge → Lambda webhook forwarder).

Each SQS record body contains the incident JSON produced by the webhook forwarder
(lambda/index.mjs). Multiple records per invocation are processed sequentially to
avoid exceeding the 15-minute Lambda timeout when running several investigations.

Execution model:
  asyncio.run(main()) drives the async LangGraph graph in a single event loop.
  boto3 and urllib calls inside graph nodes are offloaded via asyncio.to_thread()
  so the event loop stays responsive and parallel nodes run concurrently.

Post-investigation hooks (Phase 2 — live):
  _save_to_dynamodb() persists the investigation to DynamoDB.
  _post_to_slack() posts the HITL review to #sre-review and stores the thread ts.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

# Load .env automatically in local dev; silently skipped if python-dotenv is absent
# (Lambda environments never have .env files — env vars come from the function config)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agent.graph import investigation_graph
from agent.state import InvestigationState
from storage import dynamodb as db
from notifications.slack import post_hitl_review

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "sre-investigations")


# ── Phase 2 hooks ─────────────────────────────────────────────────────────────

def _save_to_dynamodb(state: InvestigationState) -> None:
    """
    Persist the completed investigation to DynamoDB.
    Non-fatal: logs on failure but does not raise.
    """
    saved = db.save_investigation(state)
    if not saved:
        logger.warning(
            "[handler] DynamoDB save failed for investigation %s — continuing",
            state.get("incident_id"),
        )


def _post_to_slack(state: InvestigationState) -> None:
    """
    Post the HITL review to #sre-review.
    On success, updates DynamoDB with the Slack thread timestamp so reaction
    events in slack_handler.py can look up the investigation.
    Non-fatal: logs on failure but does not raise.
    """
    ts = post_hitl_review(state)
    if ts:
        inv_id = state.get("incident_id", "")
        db.update_investigation(inv_id, {"slack_thread_ts": ts})
        logger.info(
            "[handler] Slack HITL posted for investigation %s, thread ts=%s",
            inv_id, ts,
        )
    else:
        logger.warning(
            "[handler] Slack post skipped/failed for investigation %s",
            state.get("incident_id"),
        )


# ── Incident parsing ──────────────────────────────────────────────────────────

def _parse_sqs_record(record: dict) -> dict | None:
    """
    Extract and parse the incident JSON from an SQS record body.
    Returns None if the record cannot be parsed (logged as error, not raised).
    """
    body = record.get("body", "")
    try:
        return json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError as exc:
        logger.error("[handler] Failed to parse SQS record body: %s — %s", body[:200], exc)
        return None


def _build_initial_state(incident: dict, incident_id: str) -> InvestigationState:
    """
    Build the initial InvestigationState from the raw incident payload.
    All investigation data fields start empty — nodes populate them.
    """
    return InvestigationState(
        incident=incident,
        incident_id=incident_id,
        alarm_details={},
        metrics=[],
        logs=[],
        cloudtrail_events=[],
        github_commits=[],
        observations=[],
        root_cause="",
        mitigation_plan="",
        investigation_gaps=[],
        status="investigating",
        approved_by=None,
        slack_thread_ts=None,
    )


# ── Core investigation runner ─────────────────────────────────────────────────

async def _run_investigation(incident: dict, incident_id: str) -> InvestigationState:
    """
    Build initial state and run the LangGraph investigation graph asynchronously.
    """
    initial_state = _build_initial_state(incident, incident_id)

    logger.info(
        "[handler] Starting investigation %s for alarm: %s",
        incident_id,
        incident.get("data", {}).get("alarmName", incident.get("title", "unknown")),
    )

    start_time = datetime.now(timezone.utc)
    final_state: InvestigationState = await investigation_graph.ainvoke(initial_state)
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

    logger.info(
        "[handler] Investigation %s complete in %.1fs — status: %s, root_cause: %s",
        incident_id,
        elapsed,
        final_state.get("status"),
        (final_state.get("root_cause") or "")[:120],
    )

    return final_state


async def _process_records(records: list[dict]) -> list[dict]:
    """
    Process all SQS records sequentially (one investigation at a time) to avoid
    exceeding Lambda timeout on bursts. Returns a list of result summaries.
    """
    results = []

    for i, record in enumerate(records):
        incident = _parse_sqs_record(record)
        if incident is None:
            results.append({"record_index": i, "status": "parse_error"})
            continue

        incident_id = f"inv-{uuid.uuid4()}"

        try:
            final_state = await _run_investigation(incident, incident_id)
            _save_to_dynamodb(final_state)
            _post_to_slack(final_state)
            results.append({
                "record_index": i,
                "incident_id":  incident_id,
                "status":       final_state.get("status"),
                "root_cause":   (final_state.get("root_cause") or "")[:200],
            })
        except Exception as exc:
            logger.exception(
                "[handler] Investigation %s failed: %s", incident_id, exc
            )
            results.append({
                "record_index": i,
                "incident_id":  incident_id,
                "status":       "error",
                "error":        str(exc),
            })

    return results


# ── Lambda handler ────────────────────────────────────────────────────────────

def handler(event: dict, context) -> dict:
    """
    Lambda entry point.

    Supports two invocation modes:
      1. SQS trigger  — event["Records"] is a list of SQS messages
      2. Direct test  — event is treated as a single incident payload
         (useful for aws lambda invoke --payload '...' local testing)
    """
    records = event.get("Records")

    if records:
        # Normal SQS path
        logger.info("[handler] Received %d SQS record(s)", len(records))
    else:
        # Direct invocation — wrap the event as a single synthetic SQS record
        logger.info("[handler] Direct invocation — treating event as single incident")
        records = [{"body": json.dumps(event)}]

    results = asyncio.run(_process_records(records))

    logger.info("[handler] Processed %d record(s): %s", len(results), results)

    return {
        "statusCode": 200,
        "body": json.dumps({"processed": len(results), "results": results}),
    }
