"""Incident search tool — semantic search over historical incidents and PagerDuty events."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

log = structlog.get_logger()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x ** 2 for x in a))
    norm_b = math.sqrt(sum(x ** 2 for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _bag_of_words(text: str) -> list[float]:
    """Simple BoW vector for offline / no-API fallback."""
    tokens = re.findall(r"\w+", text.lower())
    vocab = sorted(set(tokens))
    return [tokens.count(w) for w in vocab], vocab


def _bow_similarity(query: str, text: str) -> float:
    """Term overlap similarity — fallback when embeddings are unavailable."""
    q_tokens = set(re.findall(r"\w+", query.lower()))
    t_tokens = set(re.findall(r"\w+", text.lower()))
    if not q_tokens or not t_tokens:
        return 0.0
    return len(q_tokens & t_tokens) / len(q_tokens | t_tokens)


async def _embed(text: str) -> list[float] | None:
    """Call OpenAI embeddings API; return None if unavailable."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "text-embedding-3-small", "input": text},
            )
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
    except Exception as e:
        log.warning("embedding_failed", error=str(e))
        return None


def _load_incidents(index_path: str) -> list[dict]:
    try:
        with open(index_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


async def search_incidents(arguments: dict[str, Any]) -> str:
    """
    Semantic search over historical incident records.

    Arguments:
      query (str): Natural language search — symptom, service, error message
      limit (int): Max results to return (default 5)
      severity_filter (str): Filter by severity (SEV1, SEV2, SEV3, or ALL)
      days_back (int): Only include incidents from the last N days (0 = all)

    Returns JSON array of matching incidents ranked by similarity.
    """
    query = arguments.get("query", "")
    limit = int(arguments.get("limit", 5))
    severity_filter = arguments.get("severity_filter", "ALL").upper()
    days_back = int(arguments.get("days_back", 0))

    index_path = os.environ.get("INCIDENT_INDEX_PATH", "data/incidents.json")
    incidents = _load_incidents(index_path)

    if not incidents:
        # Fall back to PagerDuty API if no local index
        incidents = await _fetch_pagerduty_incidents(days_back or 30)

    if not incidents:
        return json.dumps({"results": [], "message": "No incident history available"})

    # Apply filters
    now = datetime.now(timezone.utc)
    filtered = []
    for inc in incidents:
        if severity_filter != "ALL" and inc.get("severity", "").upper() != severity_filter:
            continue
        if days_back > 0:
            created = inc.get("created_at") or inc.get("started_at", "")
            if created:
                try:
                    ts = datetime.fromisoformat(created.rstrip("Z")).replace(tzinfo=timezone.utc)
                    age_days = (now - ts).days
                    if age_days > days_back:
                        continue
                except ValueError:
                    pass
        filtered.append(inc)

    if not filtered:
        return json.dumps({"results": [], "message": f"No incidents match filters"})

    # Score by semantic similarity
    query_embedding = await _embed(query)
    scored = []
    for inc in filtered:
        searchable = " ".join([
            inc.get("title", ""),
            inc.get("summary", ""),
            inc.get("description", ""),
            " ".join(inc.get("affected_services", [])),
            " ".join(inc.get("tags", [])),
        ])
        if query_embedding and inc.get("embedding"):
            score = _cosine_similarity(query_embedding, inc["embedding"])
        else:
            score = _bow_similarity(query, searchable)
        scored.append((score, inc))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    results = []
    for score, inc in top:
        results.append({
            "id": inc.get("id", ""),
            "title": inc.get("title", ""),
            "severity": inc.get("severity", ""),
            "status": inc.get("status", ""),
            "started_at": inc.get("started_at") or inc.get("created_at", ""),
            "resolved_at": inc.get("resolved_at", ""),
            "affected_services": inc.get("affected_services", []),
            "summary": inc.get("summary", ""),
            "similarity_score": round(score, 3),
            "postmortem_url": inc.get("postmortem_url", ""),
        })

    log.info("incident_search", query=query, results=len(results))
    return json.dumps({
        "query": query,
        "total_searched": len(filtered),
        "results": results,
    }, indent=2)


async def get_incident_details(arguments: dict[str, Any]) -> str:
    """
    Retrieve full details for a specific incident by ID.

    Arguments:
      incident_id (str): The incident ID from search results
    """
    incident_id = arguments.get("incident_id", "")
    if not incident_id:
        return json.dumps({"error": "incident_id is required"})

    pd_token = os.environ.get("PAGERDUTY_TOKEN")
    if pd_token:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"https://api.pagerduty.com/incidents/{incident_id}",
                    headers={
                        "Authorization": f"Token token={pd_token}",
                        "Accept": "application/vnd.pagerduty+json;version=2",
                    },
                )
                r.raise_for_status()
                inc = r.json()["incident"]
                return json.dumps({
                    "id": inc["id"],
                    "title": inc["title"],
                    "status": inc["status"],
                    "urgency": inc["urgency"],
                    "created_at": inc["created_at"],
                    "resolved_at": inc.get("resolved_at"),
                    "service": inc["service"]["summary"],
                    "escalation_policy": inc["escalation_policy"]["summary"],
                    "html_url": inc["html_url"],
                    "alerts_count": inc.get("alert_counts", {}).get("all", 0),
                }, indent=2)
        except Exception as e:
            log.warning("pagerduty_fetch_failed", error=str(e), incident_id=incident_id)

    index_path = os.environ.get("INCIDENT_INDEX_PATH", "data/incidents.json")
    incidents = _load_incidents(index_path)
    for inc in incidents:
        if inc.get("id") == incident_id:
            return json.dumps(inc, indent=2)

    return json.dumps({"error": f"Incident {incident_id} not found"})


async def get_incident_stats(arguments: dict[str, Any]) -> str:
    """
    Summarise incident frequency and MTTR by service over a time window.

    Arguments:
      days (int): Lookback window in days (default 30)
      group_by (str): "service" or "severity" (default "service")
    """
    days = int(arguments.get("days", 30))
    group_by = arguments.get("group_by", "service")

    incidents = await _fetch_pagerduty_incidents(days)
    if not incidents:
        index_path = os.environ.get("INCIDENT_INDEX_PATH", "data/incidents.json")
        incidents = _load_incidents(index_path)

    now = datetime.now(timezone.utc)
    stats: dict[str, dict] = {}

    for inc in incidents:
        created_str = inc.get("created_at") or inc.get("started_at", "")
        try:
            created = datetime.fromisoformat(created_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue
        if (now - created).days > days:
            continue

        if group_by == "severity":
            key = inc.get("severity", "UNKNOWN").upper()
        else:
            services = inc.get("affected_services") or [inc.get("service", "unknown")]
            key = services[0] if services else "unknown"

        if key not in stats:
            stats[key] = {"count": 0, "total_duration_minutes": 0, "resolved_count": 0}

        stats[key]["count"] += 1
        resolved_str = inc.get("resolved_at", "")
        if resolved_str:
            try:
                resolved = datetime.fromisoformat(resolved_str.rstrip("Z")).replace(tzinfo=timezone.utc)
                duration = (resolved - created).total_seconds() / 60
                stats[key]["total_duration_minutes"] += duration
                stats[key]["resolved_count"] += 1
            except (ValueError, AttributeError):
                pass

    summary = []
    for key, s in sorted(stats.items(), key=lambda x: x[1]["count"], reverse=True):
        mttr = (
            s["total_duration_minutes"] / s["resolved_count"]
            if s["resolved_count"] > 0
            else None
        )
        summary.append({
            group_by: key,
            "incident_count": s["count"],
            "mttr_minutes": round(mttr, 1) if mttr is not None else None,
        })

    return json.dumps({
        "period_days": days,
        "group_by": group_by,
        "stats": summary,
    }, indent=2)


async def _fetch_pagerduty_incidents(days: int) -> list[dict]:
    """Fetch recent incidents from PagerDuty API."""
    pd_token = os.environ.get("PAGERDUTY_TOKEN")
    if not pd_token:
        return []
    from datetime import timedelta

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                "https://api.pagerduty.com/incidents",
                headers={
                    "Authorization": f"Token token={pd_token}",
                    "Accept": "application/vnd.pagerduty+json;version=2",
                },
                params={"since": since, "limit": 100, "statuses[]": ["resolved", "triggered", "acknowledged"]},
            )
            r.raise_for_status()
            raw = r.json()["incidents"]
            return [
                {
                    "id": inc["id"],
                    "title": inc["title"],
                    "severity": inc.get("urgency", "").upper(),
                    "status": inc["status"],
                    "created_at": inc["created_at"],
                    "resolved_at": inc.get("resolved_at"),
                    "service": inc["service"]["summary"],
                    "affected_services": [inc["service"]["summary"]],
                    "html_url": inc["html_url"],
                }
                for inc in raw
            ]
    except Exception as e:
        log.warning("pagerduty_list_failed", error=str(e))
        return []


# Tool metadata for MCP registry auto-discovery
TOOL_DEFINITIONS = [
    {
        "name": "search_incidents",
        "fn": search_incidents,
        "description": (
            "Semantic search over historical incident records. "
            "Finds past incidents matching the query by symptom, service, error message, or impact. "
            "Returns ranked results with similarity scores. Falls back to PagerDuty API if no local index."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search — e.g. 'payment latency spike', 'database connection exhaustion'",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return",
                    "default": 5,
                },
                "severity_filter": {
                    "type": "string",
                    "enum": ["ALL", "SEV1", "SEV2", "SEV3", "SEV4"],
                    "description": "Filter by incident severity",
                    "default": "ALL",
                },
                "days_back": {
                    "type": "integer",
                    "description": "Only search incidents from the last N days (0 = no limit)",
                    "default": 0,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_incident_details",
        "fn": get_incident_details,
        "description": "Get full details for a specific incident by ID, including timeline and postmortem link.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "incident_id": {
                    "type": "string",
                    "description": "Incident ID from search results or PagerDuty",
                },
            },
            "required": ["incident_id"],
        },
    },
    {
        "name": "get_incident_stats",
        "fn": get_incident_stats,
        "description": (
            "Summarise incident frequency and MTTR grouped by service or severity "
            "over a configurable lookback window. Useful for SLO review prep and "
            "identifying highest-impact services."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days",
                    "default": 30,
                },
                "group_by": {
                    "type": "string",
                    "enum": ["service", "severity"],
                    "description": "Dimension to group statistics by",
                    "default": "service",
                },
            },
            "required": [],
        },
    },
]
