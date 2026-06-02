"""
fetch_github — LangGraph node (parallel)
Fetches GitHub commits and merged PRs in the 6-hour/24-hour window before the incident.

Uses GitHub REST API v3 (no PyGitHub dependency — urllib only).
Authentication via GITHUB_TOKEN env var (Personal Access Token or GitHub App token).

Fetches:
  1. Commits to the default branch (main) in the window
  2. Recently merged PRs (closed in the window)
  3. For each commit, includes the diff stat (files changed, additions, deletions)

Environment variables:
  GITHUB_TOKEN   — required
  GITHUB_REPO    — e.g. shankarmutkekar-sonnet/agentic-sre
  GITHUB_BRANCH  — branch to query commits on (default: master)
"""

import asyncio
import json
import logging
import os
import ssl
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

from agent.state import InvestigationState

logger = logging.getLogger(__name__)

GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH     = os.environ.get("GITHUB_BRANCH", "master")
GITHUB_API_BASE   = "https://api.github.com"
COMMIT_WINDOW_MIN = 360  # look back 6 h for commits
PR_WINDOW_HOURS   = 24   # look back 24 h for merged PRs
MAX_COMMITS       = 20
MAX_PRS           = 10


def _make_github_request(path: str) -> dict | list | None:
    """
    GET {GITHUB_API_BASE}{path} with auth header.
    Returns parsed JSON or None on error.
    """
    if not GITHUB_TOKEN:
        logger.warning("[fetch_github] GITHUB_TOKEN not set")
        return None

    url = f"{GITHUB_API_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization":        f"Bearer {GITHUB_TOKEN}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent":           "agentic-sre-investigation-agent/1.0",
        },
    )
    # GitHub uses valid TLS; still create a context to allow future customisation
    ssl_ctx = ssl.create_default_context()

    try:
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("[fetch_github] GitHub API %s → HTTP %s", path, exc.code)
        return None
    except Exception as exc:
        logger.warning("[fetch_github] GitHub API error: %s", exc)
        return None


def _resolve_window(state: InvestigationState) -> tuple[datetime, datetime, datetime]:
    """Returns (commit_start, pr_start, incident_time)."""
    incident = state.get("incident", {})
    ts_str   = incident.get("timestamp") or incident.get("time", "")
    try:
        incident_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        incident_time = datetime.now(timezone.utc)

    commit_start = incident_time - timedelta(minutes=COMMIT_WINDOW_MIN)
    pr_start     = incident_time - timedelta(hours=PR_WINDOW_HOURS)
    return commit_start, pr_start, incident_time


def _fetch_commits_sync(commit_start: datetime, incident_time: datetime) -> list[dict]:
    if not GITHUB_REPO:
        return []

    since = commit_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    until = (incident_time + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    path = (
        f"/repos/{GITHUB_REPO}/commits"
        f"?sha={GITHUB_BRANCH}&since={since}&until={until}&per_page={MAX_COMMITS}"
    )
    raw = _make_github_request(path)
    if not isinstance(raw, list):
        logger.warning(
            "[fetch_github] Could not fetch commits from branch '%s' — "
            "check GITHUB_BRANCH env var (current: %s)", GITHUB_BRANCH, GITHUB_BRANCH
        )
        return []

    commits = []
    for c in raw:
        commit_data = c.get("commit", {})
        author      = commit_data.get("author", {})
        sha         = c.get("sha", "")

        # Fetch diff stats for the commit (additions/deletions/files changed)
        stats = {}
        detail = _make_github_request(f"/repos/{GITHUB_REPO}/commits/{sha}")
        if isinstance(detail, dict):
            stats = detail.get("stats", {})

        commits.append({
            "sha":        sha[:8],
            "full_sha":   sha,
            "message":    commit_data.get("message", "").split("\n")[0],  # first line only
            "author":     author.get("name", ""),
            "authored_at": author.get("date", ""),
            "url":        c.get("html_url", ""),
            "additions":  stats.get("additions", 0),
            "deletions":  stats.get("deletions", 0),
            "files_changed": stats.get("total", 0),
        })

    return commits


def _fetch_prs_sync(pr_start: datetime, incident_time: datetime) -> list[dict]:
    if not GITHUB_REPO:
        return []

    path = (
        f"/repos/{GITHUB_REPO}/pulls"
        f"?state=closed&sort=updated&direction=desc&per_page={MAX_PRS}"
    )
    raw = _make_github_request(path)
    if not isinstance(raw, list):
        return []

    prs = []
    pr_end = incident_time + timedelta(minutes=5)

    for pr in raw:
        merged_at_str = pr.get("merged_at")
        if not merged_at_str:
            continue  # not merged, just closed

        try:
            merged_at = datetime.fromisoformat(merged_at_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        if not (pr_start <= merged_at <= pr_end):
            continue  # outside our window

        prs.append({
            "number":    pr.get("number"),
            "title":     pr.get("title", ""),
            "author":    pr.get("user", {}).get("login", ""),
            "merged_at": merged_at_str,
            "url":       pr.get("html_url", ""),
            "base":      pr.get("base", {}).get("ref", ""),
            "head":      pr.get("head", {}).get("ref", ""),
            "body":      (pr.get("body") or "")[:500],  # truncate long PR bodies
        })

    return prs


def _fetch_all_sync(
    commit_start: datetime,
    pr_start: datetime,
    incident_time: datetime,
) -> tuple[list[dict], list[dict]]:
    commits = _fetch_commits_sync(commit_start, incident_time)
    prs     = _fetch_prs_sync(pr_start, incident_time)
    return commits, prs


async def run(state: InvestigationState) -> dict:
    """LangGraph async node — runs in parallel with other investigation nodes."""
    commit_start, pr_start, incident_time = _resolve_window(state)

    logger.info(
        "[fetch_github] Fetching commits since %s, PRs since %s",
        commit_start.isoformat(), pr_start.isoformat(),
    )

    commits, prs = await asyncio.to_thread(
        _fetch_all_sync, commit_start, pr_start, incident_time
    )

    all_github = [
        {"type": "commit", **c} for c in commits
    ] + [
        {"type": "pull_request", **p} for p in prs
    ]

    # ── Summarise for observation ─────────────────────────────────────────────
    gaps: list[str] = []

    if not GITHUB_TOKEN:
        gaps.append("GITHUB_TOKEN not set — GitHub commit/PR correlation unavailable")
        observation = "[fetch_github] Skipped — GITHUB_TOKEN not configured."
    elif not GITHUB_REPO:
        gaps.append("GITHUB_REPO not set — GitHub commit/PR correlation unavailable")
        observation = "[fetch_github] Skipped — GITHUB_REPO not configured."
    elif not commits and not prs:
        observation = (
            f"[fetch_github] No commits or merged PRs found in the "
            f"{COMMIT_WINDOW_MIN}-minute window before the incident. "
            "Deployment correlation: no recent changes."
        )
    else:
        commit_summary = (
            f"{len(commits)} commit(s): "
            + ", ".join(
                f"{c['sha']} by {c['author']} ({c['files_changed']} files)"
                for c in commits[:5]
            )
        ) if commits else "no commits"

        pr_summary = (
            f"{len(prs)} merged PR(s): "
            + ", ".join(f"#{p['number']} — {p['title'][:60]}" for p in prs[:3])
        ) if prs else "no merged PRs"

        observation = (
            f"[fetch_github] {commit_summary}. {pr_summary}."
        )

    logger.info("[fetch_github] %s", observation)

    return {
        "github_commits":     all_github,
        "observations":       [observation],
        "investigation_gaps": gaps,
    }
