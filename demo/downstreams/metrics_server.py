"""Minimal downstream MCP server: metrics query service.

A *real* MCP server (FastMCP over stdio). The gateway spawns this as a
subprocess and proxies namespaced tool calls (``metrics__*``) to it.

This server is restricted to the ``sre-team`` in the gateway config, so it
also demonstrates the server-level RBAC gate: a ``dev-team`` principal never
reaches this process.

Run standalone for a sanity check:

    python demo/downstreams/metrics_server.py    # speaks MCP on stdio
"""

from __future__ import annotations

import hashlib
import json

from mcp.server.fastmcp import FastMCP

server = FastMCP("metrics-downstream")

METRICS = {
    "payment_service.p99_latency_ms": "p99 request latency for payment-service",
    "payment_service.error_rate": "5xx error ratio for payment-service",
    "rds.connections_active": "active PostgreSQL connections on the primary",
}


def _series(name: str, points: int) -> list[float]:
    """Deterministic pseudo-random series so the demo output is stable."""
    seed = int(hashlib.sha256(name.encode()).hexdigest(), 16)
    out = []
    for i in range(points):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        out.append(round(50 + (seed % 1000) / 10.0, 1))
    return out


@server.tool()
def list_metrics() -> str:
    """List the metric names this service can query."""
    return json.dumps({"metrics": [{"name": n, "description": d} for n, d in METRICS.items()]}, indent=2)


@server.tool()
def query_metric(name: str, window: str = "1h") -> str:
    """Query a time series for a metric name over a window (e.g. 15m, 1h, 24h)."""
    if name not in METRICS:
        return json.dumps({"error": f"unknown metric {name!r}", "known": sorted(METRICS)})
    values = _series(f"{name}:{window}", points=6)
    return json.dumps(
        {
            "metric": name,
            "window": window,
            "unit": "ms" if "latency" in name else ("ratio" if "rate" in name else "count"),
            "datapoints": values,
            "latest": values[-1],
        },
        indent=2,
    )


if __name__ == "__main__":
    server.run()
