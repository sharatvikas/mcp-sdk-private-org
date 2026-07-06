"""Tests for incident search tool — BoW similarity, filtering, stats."""

from __future__ import annotations

import json
import os
from datetime import UTC

import pytest

os.environ.setdefault("AUTH_MODE", "apikey")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


@pytest.mark.asyncio
async def test_search_returns_empty_without_index(tmp_path):
    """When no incident index and no PD token, returns empty results gracefully."""
    os.environ["INCIDENT_INDEX_PATH"] = str(tmp_path / "nonexistent.json")
    from mcp_org.tools.incident_search import search_incidents

    result = await search_incidents({"query": "payment failure"})
    data = json.loads(result)
    assert "results" in data
    assert data["results"] == []


@pytest.mark.asyncio
async def test_search_ranks_by_bow_similarity(tmp_path):
    """BoW fallback ranks more relevant incidents higher."""
    incidents = [
        {
            "id": "INC-001",
            "title": "Database connection pool exhaustion",
            "summary": "RDS max connections exceeded, queries timing out",
            "affected_services": ["payment-service"],
            "severity": "SEV1",
            "started_at": "2024-03-15T10:00:00Z",
        },
        {
            "id": "INC-002",
            "title": "S3 bucket permission denied",
            "summary": "Access denied errors when uploading user avatars",
            "affected_services": ["user-profile"],
            "severity": "SEV3",
            "started_at": "2024-03-14T08:00:00Z",
        },
        {
            "id": "INC-003",
            "title": "High database CPU causing slow queries",
            "summary": "PostgreSQL CPU at 95%, slow query log filling up",
            "affected_services": ["analytics"],
            "severity": "SEV2",
            "started_at": "2024-03-13T15:00:00Z",
        },
    ]
    index_file = tmp_path / "incidents.json"
    index_file.write_text(json.dumps(incidents))
    os.environ["INCIDENT_INDEX_PATH"] = str(index_file)

    from mcp_org.tools.incident_search import search_incidents

    result = await search_incidents({"query": "database connection timeout"})
    data = json.loads(result)
    results = data["results"]

    assert len(results) >= 1
    # Database-related incidents should rank above S3 permission error
    titles = [r["title"] for r in results]
    db_ranks = [i for i, t in enumerate(titles) if "database" in t.lower() or "connection" in t.lower()]
    s3_ranks = [i for i, t in enumerate(titles) if "S3" in t or "s3" in t.lower()]
    if db_ranks and s3_ranks:
        assert min(db_ranks) < min(s3_ranks)


@pytest.mark.asyncio
async def test_severity_filter(tmp_path):
    """Severity filter excludes non-matching incidents."""
    incidents = [
        {
            "id": "INC-001",
            "title": "Critical outage",
            "summary": "Complete service failure",
            "affected_services": ["payment"],
            "severity": "SEV1",
            "started_at": "2024-03-15T10:00:00Z",
        },
        {
            "id": "INC-002",
            "title": "Minor degradation",
            "summary": "Slightly elevated error rate",
            "affected_services": ["api"],
            "severity": "SEV3",
            "started_at": "2024-03-14T10:00:00Z",
        },
    ]
    index_file = tmp_path / "incidents.json"
    index_file.write_text(json.dumps(incidents))
    os.environ["INCIDENT_INDEX_PATH"] = str(index_file)

    from mcp_org.tools.incident_search import search_incidents

    result = await search_incidents({"query": "outage", "severity_filter": "SEV1"})
    data = json.loads(result)
    for r in data["results"]:
        assert r["severity"] == "SEV1"


@pytest.mark.asyncio
async def test_limit_respected(tmp_path):
    """Result count does not exceed requested limit."""
    incidents = [
        {
            "id": f"INC-{i:03d}",
            "title": f"Service error {i}",
            "summary": "Database timeout",
            "affected_services": ["svc"],
            "severity": "SEV2",
            "started_at": "2024-03-15T10:00:00Z",
        }
        for i in range(20)
    ]
    index_file = tmp_path / "incidents.json"
    index_file.write_text(json.dumps(incidents))
    os.environ["INCIDENT_INDEX_PATH"] = str(index_file)

    from mcp_org.tools.incident_search import search_incidents

    result = await search_incidents({"query": "database", "limit": 3})
    data = json.loads(result)
    assert len(data["results"]) <= 3


def _iso_days_ago(days: int, hour_offset: float = 0.0) -> str:
    """ISO timestamp *days* ago (+ optional hours) — keeps stats tests inside the lookback window."""
    from datetime import datetime, timedelta

    ts = datetime.now(UTC) - timedelta(days=days) + timedelta(hours=hour_offset)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.asyncio
async def test_get_incident_stats_groups_by_service(tmp_path):
    """Stats aggregation counts incidents per service."""
    incidents = [
        {
            "id": "INC-001",
            "title": "Payment failure",
            "affected_services": ["payment-service"],
            "severity": "SEV1",
            "created_at": _iso_days_ago(3),
            "resolved_at": _iso_days_ago(3, hour_offset=1),
        },
        {
            "id": "INC-002",
            "title": "Payment timeout",
            "affected_services": ["payment-service"],
            "severity": "SEV2",
            "created_at": _iso_days_ago(4),
            "resolved_at": _iso_days_ago(4, hour_offset=0.5),
        },
        {
            "id": "INC-003",
            "title": "Auth service down",
            "affected_services": ["auth-service"],
            "severity": "SEV1",
            "created_at": _iso_days_ago(5),
            "resolved_at": _iso_days_ago(5, hour_offset=2),
        },
    ]
    index_file = tmp_path / "incidents.json"
    index_file.write_text(json.dumps(incidents))
    os.environ["INCIDENT_INDEX_PATH"] = str(index_file)

    from mcp_org.tools.incident_search import get_incident_stats

    result = await get_incident_stats({"days": 30, "group_by": "service"})
    data = json.loads(result)

    by_service = {s["service"]: s for s in data["stats"]}
    assert by_service["payment-service"]["incident_count"] == 2
    assert by_service["auth-service"]["incident_count"] == 1


@pytest.mark.asyncio
async def test_get_incident_stats_calculates_mttr(tmp_path):
    """MTTR is correctly calculated from created_at and resolved_at."""
    incidents = [
        {
            "id": "INC-001",
            "title": "DB outage",
            "affected_services": ["db-service"],
            "severity": "SEV1",
            "created_at": _iso_days_ago(2),
            "resolved_at": _iso_days_ago(2, hour_offset=2),  # 120 min
        },
    ]
    index_file = tmp_path / "incidents.json"
    index_file.write_text(json.dumps(incidents))
    os.environ["INCIDENT_INDEX_PATH"] = str(index_file)

    from mcp_org.tools.incident_search import get_incident_stats

    result = await get_incident_stats({"days": 30, "group_by": "service"})
    data = json.loads(result)

    db_stat = next(s for s in data["stats"] if s["service"] == "db-service")
    assert db_stat["mttr_minutes"] == pytest.approx(120.0, rel=0.01)
