"""
fetch_argocd — LangGraph parallel node.

Queries the ArgoCD REST API to collect:
  - Application sync status (Synced / OutOfSync / Unknown)
  - Application health status (Healthy / Degraded / Progressing / Missing)
  - Last synced Git revision (commit SHA)
  - Recent sync history (last 5 operations)
  - Active rollout / operation status

Environment variables:
  ARGOCD_URL        Base URL of the ArgoCD API server,
                    e.g. https://argocd.eks.eu-north-1.example.com
  ARGOCD_TOKEN      ArgoCD API token (Settings → Accounts → Generate Token)
  ARGOCD_APP_NAME   ArgoCD application name to query (default: flask-app)
"""

import logging
import os
import ssl
import urllib.error
import urllib.request
import json

from agent.state import InvestigationState

logger = logging.getLogger(__name__)

ARGOCD_URL      = os.environ.get("ARGOCD_URL", "")
ARGOCD_TOKEN    = os.environ.get("ARGOCD_TOKEN", "")
ARGOCD_APP_NAME = os.environ.get("ARGOCD_APP_NAME", "flask-app")

# ArgoCD API often uses self-signed certs in EKS — skip verification by default.
# Set ARGOCD_VERIFY_SSL=true to enable once a valid cert is in place.
_VERIFY_SSL = os.environ.get("ARGOCD_VERIFY_SSL", "false").lower() == "true"


def _ssl_context():
    ctx = ssl.create_default_context()
    if not _VERIFY_SSL:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(path: str) -> dict:
    url = f"{ARGOCD_URL.rstrip('/')}{path}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {ARGOCD_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=10, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode())


def _parse_app(app: dict) -> dict:
    status  = app.get("status", {})
    sync    = status.get("sync", {})
    health  = status.get("health", {})
    op      = status.get("operationState", {})
    history = status.get("history", [])

    last_5 = [
        {
            "revision":    h.get("revision", "")[:8],
            "deployed_at": h.get("deployedAt", ""),
            "source":      h.get("source", {}).get("repoURL", ""),
        }
        for h in history[-5:]
    ]

    return {
        "app_name":       app.get("metadata", {}).get("name", ARGOCD_APP_NAME),
        "sync_status":    sync.get("status", "Unknown"),
        "sync_revision":  sync.get("revision", "")[:8],
        "health_status":  health.get("status", "Unknown"),
        "health_message": health.get("message", ""),
        "operation":      op.get("phase", ""),
        "operation_msg":  op.get("message", ""),
        "sync_history":   last_5,
        "repo_url":       app.get("spec", {}).get("source", {}).get("repoURL", ""),
        "target_branch":  app.get("spec", {}).get("source", {}).get("targetRevision", ""),
    }


async def run(state: InvestigationState) -> dict:
    if not ARGOCD_URL or not ARGOCD_TOKEN:
        logger.warning("[fetch_argocd] ARGOCD_URL or ARGOCD_TOKEN not set — skipping")
        return {
            "argocd_data":  {"error": "ArgoCD not configured"},
            "observations": ["[fetch_argocd] Skipped — ARGOCD_URL/ARGOCD_TOKEN not set"],
        }

    try:
        app  = _get(f"/api/v1/applications/{ARGOCD_APP_NAME}")
        data = _parse_app(app)

        sync    = data["sync_status"]
        health  = data["health_status"]
        rev     = data["sync_revision"]
        op      = data["operation"]

        observation = (
            f"[fetch_argocd] App '{ARGOCD_APP_NAME}': "
            f"sync={sync}, health={health}, revision={rev or 'unknown'}"
            + (f", operation={op}" if op else "")
        )

        if health == "Degraded":
            observation += f" — DEGRADED: {data['health_message']}"
        if sync == "OutOfSync":
            observation += " — cluster is AHEAD of Git (manual change or failed deploy)"

        logger.info(observation)

        return {
            "argocd_data":  data,
            "observations": [observation],
        }

    except urllib.error.HTTPError as exc:
        msg = f"[fetch_argocd] HTTP {exc.code} from ArgoCD API: {exc.reason}"
        logger.error(msg)
        return {
            "argocd_data":  {"error": msg},
            "observations": [msg],
        }
    except Exception as exc:
        msg = f"[fetch_argocd] Unexpected error: {exc}"
        logger.error(msg, exc_info=True)
        return {
            "argocd_data":  {"error": msg},
            "observations": [msg],
        }
