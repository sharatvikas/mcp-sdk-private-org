"""Shared pytest fixtures for mcp-sdk-private-org tests."""

from __future__ import annotations

import json
import os
import pytest

# Set required env vars before any imports
os.environ.setdefault("AUTH_MODE", "apikey")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("TOOL_RULES", json.dumps({"*": ["*"]}))


@pytest.fixture
def api_key_headers() -> dict[str, str]:
    """Headers for API key auth in test requests."""
    return {"X-API-Key": "test-key-platform-team"}


@pytest.fixture
def team_headers() -> dict[str, str]:
    """Headers including team for RBAC tests."""
    return {
        "X-API-Key": "test-key-platform-team",
        "X-Team": "platform",
    }


@pytest.fixture
def tool_rules_all() -> dict:
    """RBAC rules granting all teams access to all tools."""
    return {"*": ["*"]}


@pytest.fixture
def tool_rules_restricted() -> dict:
    """RBAC rules restricting access to specific tools per team."""
    return {
        "platform": ["search_runbooks", "get_runbook", "list_runbook_categories"],
        "sre": ["create_postmortem_draft", "search_runbooks", "get_runbook"],
        "dev": ["search_runbooks", "list_runbook_categories"],
    }


@pytest.fixture
def sample_incident_context() -> dict:
    """Realistic incident context for postmortem and triage tests."""
    return {
        "title": "Payment Service Latency Spike",
        "severity": "SEV1",
        "started_at": "2024-03-15T14:32:00Z",
        "resolved_at": "2024-03-15T16:05:00Z",
        "affected_services": ["payment-service", "checkout-api", "fraud-detection"],
        "timeline": [
            "14:32 — PagerDuty alert: payment-service p99 > 5s",
            "14:35 — On-call acknowledges, begins triage",
            "14:45 — Identified DB connection pool exhaustion",
            "15:10 — Scaled RDS read replicas from 2→4",
            "15:40 — Deployed connection pool limit increase",
            "16:05 — p99 back to baseline <200ms, incident resolved",
        ],
        "contributing_factors": [
            "Fraud detection model rollout increased query complexity 3x",
            "Connection pool max_connections not scaled with new query patterns",
            "No canary deployment — rolled to 100% traffic simultaneously",
        ],
        "slack_thread": "https://slack.com/archives/C123/p1234567890",
        "metrics_dashboard": "https://grafana.internal/d/payment-service",
    }


@pytest.fixture
def sample_runbook_index(tmp_path) -> str:
    """Write a minimal runbook index to a temp file and return its path."""
    index = [
        {
            "id": "rb-001",
            "title": "High CPU Runbook",
            "category": "compute",
            "tags": ["cpu", "performance", "kubernetes"],
            "summary": "Diagnose and remediate high CPU usage on Kubernetes pods",
            "path": "runbooks/high-cpu.md",
        },
        {
            "id": "rb-002",
            "title": "Database Connection Pool Exhaustion",
            "category": "database",
            "tags": ["database", "postgres", "connections", "rds"],
            "summary": "Resolve database connection pool exhaustion events",
            "path": "runbooks/db-connection-pool.md",
        },
        {
            "id": "rb-003",
            "title": "Payment Service Degradation",
            "category": "payments",
            "tags": ["payments", "latency", "fraud-detection"],
            "summary": "Triage payment service latency spikes and checkout failures",
            "path": "runbooks/payment-degradation.md",
        },
    ]
    index_file = tmp_path / "runbook-index.json"
    index_file.write_text(json.dumps(index))
    return str(index_file)
