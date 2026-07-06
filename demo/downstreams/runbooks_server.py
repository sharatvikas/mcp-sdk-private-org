"""Minimal downstream MCP server: SRE runbook library.

A *real* MCP server (FastMCP over stdio). The gateway spawns this as a
subprocess and proxies namespaced tool calls (``runbooks__*``) to it.
Nothing here knows about the gateway — it is an ordinary MCP server.

Run standalone for a sanity check:

    python demo/downstreams/runbooks_server.py   # speaks MCP on stdio
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

server = FastMCP("runbooks-downstream")

# A tiny static runbook index — stand-in for a real doc store / vector index.
RUNBOOKS: dict[str, dict] = {
    "rb-001": {
        "title": "High CPU on Kubernetes pods",
        "category": "compute",
        "tags": ["cpu", "kubernetes", "performance"],
        "steps": [
            "kubectl top pods -n <ns> --sort-by=cpu",
            "Identify the hot container; check for a busy loop or GC thrash",
            "Scale the deployment or bump CPU limits, then confirm p99 recovers",
        ],
    },
    "rb-002": {
        "title": "Database connection pool exhaustion",
        "category": "database",
        "tags": ["database", "postgres", "connections", "rds"],
        "steps": [
            "Check active vs. max connections on the primary",
            "Look for leaked connections / missing pool timeouts in callers",
            "Raise pool ceiling or add read replicas; verify error rate drops",
        ],
    },
    "rb-003": {
        "title": "Payment service latency spike",
        "category": "payments",
        "tags": ["payments", "latency", "fraud-detection"],
        "steps": [
            "Correlate the spike with recent deploys / model rollouts",
            "Inspect downstream fraud-detection query latency",
            "Roll back the offending change or shed load; watch p99",
        ],
    },
}


@server.tool()
def search_runbooks(query: str) -> str:
    """Search the runbook library by symptom, alert name, or keyword."""
    q = query.lower().strip()
    matches = [
        {"id": rid, "title": rb["title"], "category": rb["category"]}
        for rid, rb in RUNBOOKS.items()
        if q in rb["title"].lower()
        or q in rb["category"].lower()
        or any(q in tag for tag in rb["tags"])
    ]
    return json.dumps({"query": query, "match_count": len(matches), "matches": matches}, indent=2)


@server.tool()
def get_runbook(id: str) -> str:
    """Fetch the full step-by-step runbook for a given runbook id (e.g. rb-002)."""
    rb = RUNBOOKS.get(id)
    if rb is None:
        return json.dumps({"error": f"no runbook with id {id!r}", "known_ids": sorted(RUNBOOKS)})
    return json.dumps({"id": id, **rb}, indent=2)


if __name__ == "__main__":
    server.run()
