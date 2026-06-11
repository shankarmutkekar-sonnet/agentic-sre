"""
synthesize — LangGraph node (sequential, runs after all parallel nodes)
Calls the configured LLM with all investigation data and parses the
structured JSON response into root_cause, mitigation_plan, and
investigation_gaps fields.

The prompt instructs the LLM to respond in strict JSON so we can
reliably extract structured fields. A plain-text fallback is applied
if JSON parsing fails.
"""

import json
import logging
import os
from datetime import datetime, timezone

from agent.state import InvestigationState
from config import get_llm

logger = logging.getLogger(__name__)

# Maximum characters sent per data section to stay within context limits.
# CloudWatch metrics and CloudTrail can be very verbose.
MAX_SECTION_CHARS = 6_000

SYNTHESIS_PROMPT = """\
You are a senior SRE investigating a production incident. Analyse all evidence \
below and respond with ONLY a valid JSON object — no markdown, no prose outside the JSON.

## Incident
{incident_summary}

## CloudWatch Alarm Details
{alarm_details}

## Metrics (last 30 minutes)
{metrics}

## Application Logs
{logs}

## CloudTrail Events (last 2 hours)
{cloudtrail_events}

## Recent GitHub Commits and Deployments
{github_commits}

## ArgoCD Application State
{argocd_data}

## Intermediate Observations (from investigation nodes)
{observations}

## Required JSON response format
{{
  "impact": "<what broke and when — include specific timestamps>",
  "root_cause": "<single most likely root cause with supporting evidence>",
  "contributing_factors": ["<factor 1>", "<factor 2>"],
  "mitigation_plan": "<step-by-step: Prepare → Pre-validate → Apply → Post-validate>",
  "investigation_gaps": ["<missing data item 1>", "<missing data item 2>"],
  "confidence": "<HIGH | MEDIUM | LOW>",
  "evidence_summary": "<2-3 sentences citing specific timestamps, commit hashes, log lines>"
}}

Rules:
- Reference exact timestamps, commit SHAs, log lines, and metric values wherever available.
- If evidence is insufficient for a field, say so explicitly rather than guessing.
- investigation_gaps must list every data source that was unavailable or empty.
"""


def _truncate(obj, max_chars: int = MAX_SECTION_CHARS) -> str:
    """Serialise obj to JSON string, truncated to max_chars with a notice."""
    text = json.dumps(obj, indent=2, default=str)
    if len(text) > max_chars:
        truncated = text[:max_chars]
        return truncated + f"\n... [TRUNCATED — {len(text) - max_chars} chars omitted]"
    return text


def _build_incident_summary(state: InvestigationState) -> str:
    incident = state.get("incident", {})
    return (
        f"Incident ID : {state.get('incident_id', 'unknown')}\n"
        f"Title       : {incident.get('title', 'N/A')}\n"
        f"Service     : {incident.get('service', 'N/A')}\n"
        f"Priority    : {incident.get('priority', 'N/A')}\n"
        f"Action      : {incident.get('action', 'N/A')}\n"
        f"Timestamp   : {incident.get('timestamp', incident.get('time', 'N/A'))}\n"
        f"Description : {incident.get('description', 'N/A')}"
    )


def _build_prompt(state: InvestigationState) -> str:
    return SYNTHESIS_PROMPT.format(
        incident_summary  = _build_incident_summary(state),
        alarm_details     = _truncate(state.get("alarm_details", {})),
        metrics           = _truncate(state.get("metrics", [])),
        logs              = _truncate(state.get("logs", []), max_chars=8_000),
        cloudtrail_events = _truncate(state.get("cloudtrail_events", [])),
        github_commits    = _truncate(state.get("github_commits", [])),
        argocd_data       = _truncate(state.get("argocd_data", {})),
        observations      = "\n".join(state.get("observations", [])),
    )


def _parse_llm_response(content: str) -> dict:
    """
    Extract and parse the JSON object from the LLM response.
    Handles cases where the model wraps it in ```json ... ``` fences.
    """
    text = content.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text  = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find the first { ... } block
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass

    # Complete parse failure — return raw content under root_cause
    logger.warning("[synthesize] Could not parse LLM response as JSON — using raw text")
    return {
        "impact":               "See root_cause for full analysis",
        "root_cause":           content,
        "contributing_factors": [],
        "mitigation_plan":      "See root_cause for full analysis",
        "investigation_gaps":   [],
        "confidence":           "LOW",
        "evidence_summary":     "JSON parsing failed — raw LLM output stored in root_cause",
    }


async def run(state: InvestigationState) -> dict:
    """
    LangGraph async node — runs after all parallel investigation nodes complete.
    Calls the LLM synchronously inside asyncio.to_thread to keep the event loop free.
    """
    import asyncio

    logger.info("[synthesize] Building prompt and calling LLM …")
    prompt = _build_prompt(state)

    llm = get_llm()

    def _call_llm() -> str:
        from langchain_core.messages import HumanMessage
        response = llm.invoke([HumanMessage(content=prompt)])
        return response.content

    raw_response = await asyncio.to_thread(_call_llm)
    logger.info("[synthesize] LLM responded (%d chars)", len(raw_response))

    parsed = _parse_llm_response(raw_response)

    # Merge LLM-reported gaps with gaps already collected by investigation nodes
    llm_gaps   = parsed.get("investigation_gaps", [])
    state_gaps = state.get("investigation_gaps", [])
    all_gaps   = list(dict.fromkeys(state_gaps + llm_gaps))  # deduplicate, preserve order

    observation = (
        f"[synthesize] LLM analysis complete. "
        f"Confidence: {parsed.get('confidence', 'UNKNOWN')}. "
        f"Root cause: {parsed.get('root_cause', '')[:200]}"
    )
    logger.info("[synthesize] %s", observation)

    return {
        "root_cause":         parsed.get("root_cause", ""),
        "mitigation_plan":    parsed.get("mitigation_plan", ""),
        "investigation_gaps": llm_gaps,   # reducer adds these to node-collected gaps
        "observations":       [observation],
        "status":             "pending_review",
    }
