"""
InvestigationState — shared state passed between every LangGraph node.

List fields written by PARALLEL nodes use operator.add as a reducer so
LangGraph concatenates partial updates instead of overwriting them.

Fields written by only one node (alarm_details, metrics, logs, etc.) use
plain types — last write wins, which is safe for single-writer fields.
"""

import operator
from typing import Annotated, List, Optional, TypedDict


class InvestigationState(TypedDict):
    # ── Input ────────────────────────────────────────────────────────────────
    # Raw webhook payload forwarded from CloudWatch → EventBridge → Lambda
    incident: dict

    # Unique identifier used as DynamoDB PK (generated in handler.py)
    incident_id: str

    # ── Investigation data (each populated by exactly one node) ───────────────
    alarm_details: dict          # fetch_alarm node
    metrics: List[dict]          # fetch_metrics node
    logs: List[dict]             # fetch_logs node
    cloudtrail_events: List[dict]  # fetch_cloudtrail node
    github_commits: List[dict]   # fetch_github node

    # ── Synthesis output ──────────────────────────────────────────────────────
    # REDUCER: each parallel node appends its own string; LangGraph adds the lists
    observations: Annotated[List[str], operator.add]

    # Populated by synthesize node (single writer — plain type is fine)
    root_cause: str
    mitigation_plan: str

    # REDUCER: parallel nodes may each append gaps; LangGraph adds the lists
    investigation_gaps: Annotated[List[str], operator.add]

    # ── Metadata ──────────────────────────────────────────────────────────────
    # investigating → pending_review → published | dismissed
    status: str

    # Slack user ID of the reviewer who reacted ✅ or ❌
    approved_by: Optional[str]

    # Slack message timestamp of the HITL review thread
    slack_thread_ts: Optional[str]
