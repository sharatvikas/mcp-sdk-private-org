"""Health check and Prometheus metrics HTTP server.

Runs alongside the MCP gateway as a lightweight asyncio HTTP server.
Designed for Kubernetes liveness/readiness probes and basic observability.

Endpoints:
    GET /health  — Liveness probe. Returns 200 OK if the event loop is alive.
    GET /ready   — Readiness probe. Returns 200 OK if registry loaded + auth configured.
    GET /metrics — Prometheus-format metrics: tool call counts, latency histograms.

Usage (in your entrypoint)::

    from mcp_org.health import HealthServer
    health = HealthServer(gateway)
    asyncio.gather(gateway.run_stdio(), health.start(port=8080))
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    pass

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Metrics store (in-process; swap for Prometheus client lib for production)
# ---------------------------------------------------------------------------

@dataclass
class _ToolMetrics:
    calls_total: int = 0
    errors_total: int = 0
    # Store up to 1000 latency samples for percentile approximation
    latency_samples_ms: list[float] = field(default_factory=list)
    _MAX_SAMPLES: int = field(default=1000, init=False, repr=False)

    def record(self, duration_ms: float, success: bool) -> None:
        self.calls_total += 1
        if not success:
            self.errors_total += 1
        if len(self.latency_samples_ms) >= self._MAX_SAMPLES:
            # Drop oldest quarter to make room
            self.latency_samples_ms = self.latency_samples_ms[self._MAX_SAMPLES // 4 :]
        self.latency_samples_ms.append(duration_ms)

    def p50(self) -> float:
        return _percentile(self.latency_samples_ms, 50)

    def p95(self) -> float:
        return _percentile(self.latency_samples_ms, 95)

    def p99(self) -> float:
        return _percentile(self.latency_samples_ms, 99)


def _percentile(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    sorted_s = sorted(samples)
    k = (len(sorted_s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_s) - 1)
    return sorted_s[lo] + (sorted_s[hi] - sorted_s[lo]) * (k - lo)


class MetricsRegistry:
    """Thread-safe (asyncio-safe) in-process metrics store."""

    def __init__(self) -> None:
        self._tools: dict[str, _ToolMetrics] = defaultdict(lambda: _ToolMetrics())
        self._start_time = time.monotonic()
        self._gateway_ready = False
        self._registry_tool_count = 0
        # Generic labelled counters/gauges, e.g. rate-limit denials or
        # downstream health. Keyed by (metric_name, sorted label tuple).
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._metric_help: dict[str, str] = {}

    def record_call(self, tool_name: str, duration_ms: float, success: bool) -> None:
        self._tools[tool_name].record(duration_ms, success)

    def inc_counter(self, name: str, amount: float = 1.0, help_text: str = "", **labels: str) -> None:
        """Increment a labelled counter (created on first use)."""
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        self._counters[key] += amount
        if help_text:
            self._metric_help.setdefault(name, help_text)

    def set_gauge(self, name: str, value: float, help_text: str = "", **labels: str) -> None:
        """Set a labelled gauge to an absolute value."""
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        self._gauges[key] = value
        if help_text:
            self._metric_help.setdefault(name, help_text)

    def get_counter(self, name: str, **labels: str) -> float:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        return self._counters.get(key, 0.0)

    def get_gauge(self, name: str, **labels: str) -> float | None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        return self._gauges.get(key)

    @staticmethod
    def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        inner = ",".join(f'{k}="{v}"' for k, v in labels)
        return "{" + inner + "}"

    def set_ready(self, ready: bool, tool_count: int = 0) -> None:
        self._gateway_ready = ready
        self._registry_tool_count = tool_count

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start_time

    @property
    def is_ready(self) -> bool:
        return self._gateway_ready

    def to_prometheus(self) -> str:
        """Render metrics in Prometheus text exposition format."""
        lines: list[str] = [
            "# HELP mcp_gateway_uptime_seconds Time since gateway started",
            "# TYPE mcp_gateway_uptime_seconds gauge",
            f"mcp_gateway_uptime_seconds {self.uptime_seconds:.2f}",
            "",
            "# HELP mcp_gateway_ready Whether gateway passed readiness checks",
            "# TYPE mcp_gateway_ready gauge",
            f"mcp_gateway_ready {1 if self._gateway_ready else 0}",
            "",
            "# HELP mcp_tool_registry_size Number of tools registered",
            "# TYPE mcp_tool_registry_size gauge",
            f"mcp_tool_registry_size {self._registry_tool_count}",
            "",
            "# HELP mcp_tool_calls_total Total tool invocations by tool name",
            "# TYPE mcp_tool_calls_total counter",
        ]
        for name, m in sorted(self._tools.items()):
            lines.append(f'mcp_tool_calls_total{{tool="{name}"}} {m.calls_total}')

        lines += [
            "",
            "# HELP mcp_tool_errors_total Total tool errors by tool name",
            "# TYPE mcp_tool_errors_total counter",
        ]
        for name, m in sorted(self._tools.items()):
            lines.append(f'mcp_tool_errors_total{{tool="{name}"}} {m.errors_total}')

        lines += [
            "",
            "# HELP mcp_tool_latency_p50_ms P50 latency in milliseconds",
            "# TYPE mcp_tool_latency_p50_ms gauge",
        ]
        for name, m in sorted(self._tools.items()):
            lines.append(f'mcp_tool_latency_p50_ms{{tool="{name}"}} {m.p50():.2f}')

        lines += [
            "",
            "# HELP mcp_tool_latency_p95_ms P95 latency in milliseconds",
            "# TYPE mcp_tool_latency_p95_ms gauge",
        ]
        for name, m in sorted(self._tools.items()):
            lines.append(f'mcp_tool_latency_p95_ms{{tool="{name}"}} {m.p95():.2f}')

        lines += [
            "",
            "# HELP mcp_tool_latency_p99_ms P99 latency in milliseconds",
            "# TYPE mcp_tool_latency_p99_ms gauge",
        ]
        for name, m in sorted(self._tools.items()):
            lines.append(f'mcp_tool_latency_p99_ms{{tool="{name}"}} {m.p99():.2f}')

        # Generic labelled counters and gauges (grouped by metric name)
        for kind, store in (("counter", self._counters), ("gauge", self._gauges)):
            by_name: dict[str, list[tuple[tuple[tuple[str, str], ...], float]]] = defaultdict(list)
            for (metric_name, labels), value in store.items():
                by_name[metric_name].append((labels, value))
            for metric_name in sorted(by_name):
                lines += [
                    "",
                    f"# HELP {metric_name} {self._metric_help.get(metric_name, metric_name)}",
                    f"# TYPE {metric_name} {kind}",
                ]
                for labels, value in sorted(by_name[metric_name]):
                    lines.append(f"{metric_name}{self._render_labels(labels)} {value:g}")

        return "\n".join(lines) + "\n"

    def to_health_json(self) -> dict[str, Any]:
        total_calls = sum(m.calls_total for m in self._tools.values())
        total_errors = sum(m.errors_total for m in self._tools.values())
        return {
            "status": "ok",
            "uptime_seconds": round(self.uptime_seconds, 2),
            "total_calls": total_calls,
            "total_errors": total_errors,
            "error_rate": round(total_errors / max(total_calls, 1), 4),
        }

    def to_ready_json(self, checks: dict[str, bool]) -> dict[str, Any]:
        all_ok = all(checks.values())
        return {
            "status": "ready" if all_ok else "not_ready",
            "checks": checks,
            "tool_count": self._registry_tool_count,
        }


# Global singleton — imported by gateway.py
metrics = MetricsRegistry()


# ---------------------------------------------------------------------------
# Minimal async HTTP server (no external framework dependency)
# ---------------------------------------------------------------------------

class HealthServer:
    """Lightweight asyncio HTTP server for health + metrics endpoints.

    Handles only GET /health, GET /ready, GET /metrics.
    Everything else returns 404.
    """

    def __init__(self, gateway: Any | None = None) -> None:
        self._gateway = gateway

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        server = await asyncio.start_server(
            self._handle_connection, host, port
        )
        log.info("health_server.started", host=host, port=port)
        async with server:
            await server.serve_forever()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = (await reader.readline()).decode().strip()
            # Drain headers
            while True:
                line = (await reader.readline()).decode().strip()
                if not line:
                    break

            method, path, *_ = (request_line.split() + ["", ""])[:3]
            path = path.split("?")[0]  # strip query string

            if method != "GET":
                await self._respond(writer, 405, "text/plain", b"Method Not Allowed")
                return

            if path == "/health":
                body = json.dumps(metrics.to_health_json(), indent=2).encode()
                await self._respond(writer, 200, "application/json", body)

            elif path == "/ready":
                checks = await self._readiness_checks()
                data = metrics.to_ready_json(checks)
                status = 200 if data["status"] == "ready" else 503
                body = json.dumps(data, indent=2).encode()
                await self._respond(writer, status, "application/json", body)

            elif path == "/metrics":
                body = metrics.to_prometheus().encode()
                await self._respond(writer, 200, "text/plain; version=0.0.4", body)

            else:
                await self._respond(writer, 404, "text/plain", b"Not Found")

        except Exception as exc:
            log.warning("health_server.error", error=str(exc))
        finally:
            writer.close()

    async def _readiness_checks(self) -> dict[str, bool]:
        checks: dict[str, bool] = {}

        # 1. Registry: tool count > 0 means it loaded successfully
        checks["registry_loaded"] = metrics._registry_tool_count > 0

        # 2. Auth: check that required env vars are present
        auth_mode = __import__("os").environ.get("AUTH_MODE", "apikey")
        if auth_mode == "oidc":
            checks["auth_configured"] = bool(
                __import__("os").environ.get("OIDC_ISSUER")
                and __import__("os").environ.get("OIDC_CLIENT_ID")
            )
        else:
            # apikey mode: must have at least one key configured
            keys_json = __import__("os").environ.get("ORG_API_KEYS", "")
            checks["auth_configured"] = bool(keys_json)

        # 3. Event loop is alive (implicit — we're responding)
        checks["event_loop"] = True

        return checks

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: int,
        content_type: str,
        body: bytes,
    ) -> None:
        status_text = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}.get(
            status, "Unknown"
        )
        response = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode() + body
        writer.write(response)
        await writer.drain()
