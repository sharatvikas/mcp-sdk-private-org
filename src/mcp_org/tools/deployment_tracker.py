"""Deployment tracker tool — track recent deployments and correlate with incidents.

Integrates with GitHub Deployments API and Argo CD to surface what changed
recently, helping SREs quickly answer "did a deployment cause this incident?"
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
_GITHUB_ORG = os.environ.get("GITHUB_ORG", "")
_ARGOCD_URL = os.environ.get("ARGOCD_URL", "")
_ARGOCD_TOKEN = os.environ.get("ARGOCD_TOKEN", "")


async def list_recent_deployments(arguments: dict[str, Any]) -> str:
    """
    List recent deployments across GitHub repos and/or Argo CD apps.

    Arguments:
      hours_back (int): Look-back window in hours (default 24)
      environment (str): Filter by environment (production, staging, etc.)
      repo (str): Filter by repo name (optional, searches all org repos if omitted)
      include_argocd (bool): Include Argo CD sync events (default true)

    Returns JSON array of deployments sorted newest first.
    """
    hours_back = int(arguments.get("hours_back", 24))
    environment = arguments.get("environment", "")
    repo_filter = arguments.get("repo", "")
    include_argocd = bool(arguments.get("include_argocd", True))

    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    deployments: list[dict] = []

    # GitHub Deployments API
    if _GITHUB_TOKEN and _GITHUB_ORG:
        gh_deployments = await _fetch_github_deployments(since, environment, repo_filter)
        deployments.extend(gh_deployments)

    # Argo CD sync history
    if include_argocd and _ARGOCD_URL and _ARGOCD_TOKEN:
        argo_deployments = await _fetch_argocd_syncs(since, environment)
        deployments.extend(argo_deployments)

    # Sort newest first
    deployments.sort(key=lambda d: d.get("deployed_at", ""), reverse=True)

    log.info("deployment_list", count=len(deployments), hours_back=hours_back)
    return json.dumps({
        "since": since.isoformat(),
        "environment_filter": environment or "all",
        "total": len(deployments),
        "deployments": deployments,
    }, indent=2)


async def get_deployment_diff(arguments: dict[str, Any]) -> str:
    """
    Get the code diff and commit list between the current and previous deployment.

    Arguments:
      repo (str): Repository name (owner/repo format)
      deployment_id (str): Deployment ID from list_recent_deployments output

    Returns the commit list and a summary of what changed.
    """
    repo = arguments.get("repo", "")
    deployment_id = arguments.get("deployment_id", "")

    if not repo or not deployment_id:
        return json.dumps({"error": "repo and deployment_id are required"})

    if not _GITHUB_TOKEN:
        return json.dumps({"error": "GITHUB_TOKEN not configured"})

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            headers = {
                "Authorization": f"Bearer {_GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }

            # Get current deployment
            r = await client.get(
                f"https://api.github.com/repos/{repo}/deployments/{deployment_id}",
                headers=headers,
            )
            r.raise_for_status()
            current = r.json()
            current_sha = current.get("sha", "")
            environment = current.get("environment", "")

            # Find the previous successful deployment to this environment
            r = await client.get(
                f"https://api.github.com/repos/{repo}/deployments",
                headers=headers,
                params={"environment": environment, "per_page": 10},
            )
            r.raise_for_status()
            all_deployments = r.json()

            prev_sha = None
            found_current = False
            for dep in all_deployments:
                if dep["id"] == int(deployment_id):
                    found_current = True
                    continue
                if found_current:
                    # Get status to confirm it was successful
                    sr = await client.get(
                        f"https://api.github.com/repos/{repo}/deployments/{dep['id']}/statuses",
                        headers=headers,
                    )
                    if sr.status_code == 200:
                        statuses = sr.json()
                        if statuses and statuses[0].get("state") == "success":
                            prev_sha = dep["sha"]
                            break

            if not prev_sha:
                return json.dumps({
                    "deployment_id": deployment_id,
                    "sha": current_sha,
                    "message": "No previous successful deployment found — this may be the first deploy",
                })

            # Get comparison
            r = await client.get(
                f"https://api.github.com/repos/{repo}/compare/{prev_sha}...{current_sha}",
                headers=headers,
            )
            r.raise_for_status()
            comparison = r.json()

            commits = [
                {
                    "sha": c["sha"][:8],
                    "message": c["commit"]["message"].split("\n")[0],
                    "author": c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"],
                    "url": c["html_url"],
                }
                for c in comparison.get("commits", [])
            ]

            files_changed = [
                {
                    "filename": f["filename"],
                    "status": f["status"],
                    "additions": f["additions"],
                    "deletions": f["deletions"],
                }
                for f in comparison.get("files", [])[:20]  # cap at 20 files
            ]

            return json.dumps({
                "repo": repo,
                "environment": environment,
                "current_sha": current_sha[:8],
                "previous_sha": prev_sha[:8],
                "commits_count": comparison.get("total_commits", 0),
                "files_changed_count": comparison.get("total_commits", len(files_changed)),
                "commits": commits,
                "files_changed": files_changed,
                "compare_url": comparison.get("html_url", ""),
            }, indent=2)

    except Exception as e:
        log.warning("deployment_diff_failed", error=str(e))
        return json.dumps({"error": str(e)})


async def correlate_deployment_with_incident(arguments: dict[str, Any]) -> str:
    """
    Given an incident start time and affected service, find deployments that
    preceded the incident and assess their likelihood as root cause.

    Arguments:
      incident_start (str): ISO 8601 timestamp when the incident started
      service (str): Affected service name (maps to GitHub repo or Argo CD app)
      lookback_hours (int): How many hours before incident to scan (default 2)

    Returns ranked list of candidate deployments with risk assessment.
    """
    incident_start_str = arguments.get("incident_start", "")
    service = arguments.get("service", "")
    lookback_hours = int(arguments.get("lookback_hours", 2))

    if not incident_start_str:
        return json.dumps({"error": "incident_start is required (ISO 8601 format)"})

    try:
        incident_start = datetime.fromisoformat(incident_start_str.rstrip("Z")).replace(tzinfo=timezone.utc)
    except ValueError as e:
        return json.dumps({"error": f"Invalid incident_start format: {e}"})

    scan_from = incident_start - timedelta(hours=lookback_hours)

    deployments_result = await list_recent_deployments({
        "hours_back": lookback_hours + 1,
        "repo": service,
    })
    all_deployments = json.loads(deployments_result).get("deployments", [])

    # Filter to deployments in the window before the incident
    candidates = []
    for dep in all_deployments:
        dep_time_str = dep.get("deployed_at", "")
        if not dep_time_str:
            continue
        try:
            dep_time = datetime.fromisoformat(dep_time_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if scan_from <= dep_time <= incident_start:
            minutes_before = int((incident_start - dep_time).total_seconds() / 60)
            risk = "HIGH" if minutes_before < 15 else "MEDIUM" if minutes_before < 60 else "LOW"
            candidates.append({
                **dep,
                "minutes_before_incident": minutes_before,
                "deployment_risk": risk,
                "correlation_note": (
                    f"Deployed {minutes_before} min before incident — "
                    + ("very likely related" if risk == "HIGH"
                       else "possibly related" if risk == "MEDIUM"
                       else "less likely related")
                ),
            })

    # Sort by proximity to incident (closest first)
    candidates.sort(key=lambda d: d["minutes_before_incident"])

    return json.dumps({
        "incident_start": incident_start_str,
        "service": service or "all",
        "lookback_hours": lookback_hours,
        "candidate_deployments": len(candidates),
        "results": candidates,
        "recommendation": (
            f"Found {len(candidates)} deployment(s) in the {lookback_hours}h window. "
            + ("Most likely cause: " + candidates[0].get("repo", "") + " — " + candidates[0].get("correlation_note", "")
               if candidates else "No deployments found in the window — look for config changes or external factors.")
        ),
    }, indent=2)


async def _fetch_github_deployments(
    since: datetime,
    environment: str,
    repo_filter: str,
) -> list[dict]:
    """Fetch deployment events from GitHub Deployments API."""
    if not _GITHUB_TOKEN or not _GITHUB_ORG:
        return []

    headers = {
        "Authorization": f"Bearer {_GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params: dict[str, Any] = {"per_page": 100}
    if environment:
        params["environment"] = environment

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # Search repos
            if repo_filter:
                repos = [f"{_GITHUB_ORG}/{repo_filter}"]
            else:
                r = await client.get(
                    f"https://api.github.com/orgs/{_GITHUB_ORG}/repos",
                    headers=headers,
                    params={"per_page": 50, "sort": "pushed"},
                )
                r.raise_for_status()
                repos = [repo["full_name"] for repo in r.json()]

            deployments: list[dict] = []
            for repo in repos[:20]:  # Cap to avoid rate limits
                try:
                    r = await client.get(
                        f"https://api.github.com/repos/{repo}/deployments",
                        headers=headers,
                        params=params,
                    )
                    if r.status_code != 200:
                        continue

                    for dep in r.json():
                        created = dep.get("created_at", "")
                        try:
                            dep_time = datetime.fromisoformat(created.rstrip("Z")).replace(tzinfo=timezone.utc)
                            if dep_time < since:
                                continue
                        except ValueError:
                            continue

                        deployments.append({
                            "source": "github",
                            "repo": repo,
                            "deployment_id": str(dep["id"]),
                            "environment": dep.get("environment", ""),
                            "sha": dep.get("sha", "")[:8],
                            "deployed_at": created,
                            "deployed_by": dep.get("creator", {}).get("login", ""),
                            "description": dep.get("description", ""),
                            "url": dep.get("url", ""),
                        })
                except Exception:
                    continue

            return deployments

    except Exception as e:
        log.warning("github_deployments_failed", error=str(e))
        return []


async def _fetch_argocd_syncs(since: datetime, environment: str) -> list[dict]:
    """Fetch recent Argo CD sync events."""
    if not _ARGOCD_URL or not _ARGOCD_TOKEN:
        return []

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_ARGOCD_URL.rstrip('/')}/api/v1/applications",
                headers={"Authorization": f"Bearer {_ARGOCD_TOKEN}"},
                params={"fields": "items.metadata.name,items.status.history,items.spec.destination"},
            )
            r.raise_for_status()
            apps = r.json().get("items", [])

            deployments: list[dict] = []
            for app in apps:
                app_name = app.get("metadata", {}).get("name", "")
                app_env = app.get("spec", {}).get("destination", {}).get("namespace", "")

                if environment and environment not in (app_name, app_env):
                    continue

                for sync in app.get("status", {}).get("history", []):
                    deployed_at = sync.get("deployedAt", "")
                    try:
                        dep_time = datetime.fromisoformat(deployed_at.rstrip("Z")).replace(tzinfo=timezone.utc)
                        if dep_time < since:
                            continue
                    except (ValueError, AttributeError):
                        continue

                    deployments.append({
                        "source": "argocd",
                        "repo": app_name,
                        "deployment_id": str(sync.get("id", "")),
                        "environment": app_env,
                        "sha": sync.get("revision", "")[:8],
                        "deployed_at": deployed_at,
                        "deployed_by": "argocd",
                        "description": sync.get("source", {}).get("repoURL", ""),
                    })

            return deployments

    except Exception as e:
        log.warning("argocd_syncs_failed", error=str(e))
        return []


# Tool metadata for MCP registry auto-discovery
TOOL_DEFINITIONS = [
    {
        "name": "list_recent_deployments",
        "fn": list_recent_deployments,
        "description": (
            "List recent deployments across GitHub repos and Argo CD apps. "
            "Returns deployments sorted newest first with SHA, environment, deployer, and timestamp. "
            "Use this when an incident starts to quickly see what changed recently."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours_back": {"type": "integer", "description": "Look-back window in hours", "default": 24},
                "environment": {"type": "string", "description": "Filter by environment (production, staging)"},
                "repo": {"type": "string", "description": "Filter by repository name"},
                "include_argocd": {"type": "boolean", "description": "Include Argo CD sync events", "default": True},
            },
            "required": [],
        },
    },
    {
        "name": "get_deployment_diff",
        "fn": get_deployment_diff,
        "description": "Get commits and files changed between the current and previous deployment for a repo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "deployment_id": {"type": "string", "description": "Deployment ID from list_recent_deployments"},
            },
            "required": ["repo", "deployment_id"],
        },
    },
    {
        "name": "correlate_deployment_with_incident",
        "fn": correlate_deployment_with_incident,
        "description": (
            "Given an incident start time and affected service, find deployments in the preceding "
            "window and rank them by likelihood of being the root cause. "
            "Returns each deployment's proximity to the incident and a risk assessment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "incident_start": {"type": "string", "description": "ISO 8601 incident start time"},
                "service": {"type": "string", "description": "Affected service/repo name"},
                "lookback_hours": {"type": "integer", "description": "Hours before incident to scan", "default": 2},
            },
            "required": ["incident_start"],
        },
    },
]
