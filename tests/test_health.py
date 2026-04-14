"""Tests for the health check HTTP server and metrics registry."""

import asyncio
import json
import time

import pytest

from mcp_org.health import MetricsRegistry, HealthServer, _percentile


class TestPercentile:
    def test_empty_list_returns_zero(self):
        assert _percentile([], 50) == 0.0

    def test_single_element(self):
        assert _percentile([42.0], 50) == 42.0
        assert _percentile([42.0], 99) == 42.0

    def test_p50_of_sorted_list(self):
        samples = [10.0, 20.0, 30.0, 40.0, 50.0]
        p50 = _percentile(samples, 50)
        assert 28.0 <= p50 <= 32.0  # midpoint of sorted list

    def test_p99_higher_than_p50(self):
        samples = list(range(1, 101, dtype=float)) if False else [float(i) for i in range(1, 101)]
        assert _percentile(samples, 99) > _percentile(samples, 50)


class TestMetricsRegistry:
    def setup_method(self):
        self.reg = MetricsRegistry()

    def test_initial_state(self):
        assert self.reg.uptime_seconds >= 0.0
        assert not self.reg.is_ready

    def test_set_ready(self):
        self.reg.set_ready(True, tool_count=12)
        assert self.reg.is_ready
        assert self.reg._registry_tool_count == 12

    def test_record_call_increments_totals(self):
        self.reg.record_call("my_tool", 50.0, success=True)
        self.reg.record_call("my_tool", 80.0, success=True)
        self.reg.record_call("my_tool", 120.0, success=False)
        m = self.reg._tools["my_tool"]
        assert m.calls_total == 3
        assert m.errors_total == 1

    def test_record_call_computes_latencies(self):
        for ms in [10.0, 20.0, 30.0, 40.0, 50.0]:
            self.reg.record_call("slow_tool", ms, success=True)
        m = self.reg._tools["slow_tool"]
        assert m.p50() > 0
        assert m.p99() >= m.p50()

    def test_to_prometheus_format(self):
        self.reg.set_ready(True, tool_count=5)
        self.reg.record_call("search_runbooks", 25.0, success=True)
        self.reg.record_call("create_postmortem_draft", 300.0, success=False)
        prom = self.reg.to_prometheus()
        assert "mcp_gateway_ready 1" in prom
        assert "mcp_tool_registry_size 5" in prom
        assert 'mcp_tool_calls_total{tool="search_runbooks"} 1' in prom
        assert 'mcp_tool_errors_total{tool="create_postmortem_draft"} 1' in prom
        assert "mcp_tool_latency_p99_ms" in prom

    def test_to_health_json(self):
        self.reg.record_call("t1", 10.0, success=True)
        self.reg.record_call("t1", 20.0, success=False)
        data = self.reg.to_health_json()
        assert data["status"] == "ok"
        assert data["total_calls"] == 2
        assert data["total_errors"] == 1
        assert 0.0 <= data["error_rate"] <= 1.0

    def test_to_ready_json_all_ok(self):
        self.reg.set_ready(True, tool_count=3)
        data = self.reg.to_ready_json({"registry_loaded": True, "auth_configured": True, "event_loop": True})
        assert data["status"] == "ready"
        assert data["tool_count"] == 3

    def test_to_ready_json_not_ready(self):
        data = self.reg.to_ready_json({"registry_loaded": False, "auth_configured": True, "event_loop": True})
        assert data["status"] == "not_ready"

    def test_latency_sample_cap(self):
        """Samples should be capped to prevent unbounded memory growth."""
        m = self.reg._tools["capped"]
        for i in range(1500):
            m.record(float(i), True)
        # After capping, should be under the max
        assert len(m.latency_samples_ms) <= m._MAX_SAMPLES


class TestHealthServerResponses:
    """Integration tests: spin up a real server and hit it with raw TCP."""

    async def _send_request(self, port: int, path: str) -> tuple[int, str, str]:
        """Returns (status_code, content_type, body)."""
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        response = b""
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
                if not chunk:
                    break
                response += chunk
        except asyncio.TimeoutError:
            pass
        writer.close()
        text = response.decode(errors="replace")
        lines = text.split("\r\n")
        status = int(lines[0].split()[1]) if lines else 0
        ct = next((l.split(": ", 1)[1] for l in lines if l.lower().startswith("content-type:")), "")
        body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
        return status, ct, body

    def test_health_returns_200(self):
        from mcp_org.health import metrics
        metrics.set_ready(True, tool_count=3)

        port = 18081

        async def run():
            server = HealthServer()
            srv_task = asyncio.create_task(server.start("127.0.0.1", port))
            await asyncio.sleep(0.1)
            status, ct, body = await self._send_request(port, "/health")
            srv_task.cancel()
            return status, ct, body

        status, ct, body = asyncio.run(run())
        assert status == 200
        assert "application/json" in ct
        data = json.loads(body)
        assert data["status"] == "ok"

    def test_metrics_returns_prometheus_text(self):
        from mcp_org.health import metrics
        metrics.record_call("test_tool", 42.0, success=True)

        port = 18082

        async def run():
            server = HealthServer()
            srv_task = asyncio.create_task(server.start("127.0.0.1", port))
            await asyncio.sleep(0.1)
            status, ct, body = await self._send_request(port, "/metrics")
            srv_task.cancel()
            return status, ct, body

        status, ct, body = asyncio.run(run())
        assert status == 200
        assert "text/plain" in ct
        assert "mcp_gateway_uptime_seconds" in body
        assert "mcp_tool_calls_total" in body

    def test_unknown_path_returns_404(self):
        port = 18083

        async def run():
            server = HealthServer()
            srv_task = asyncio.create_task(server.start("127.0.0.1", port))
            await asyncio.sleep(0.1)
            status, _, _ = await self._send_request(port, "/unknown")
            srv_task.cancel()
            return status

        status = asyncio.run(run())
        assert status == 404
