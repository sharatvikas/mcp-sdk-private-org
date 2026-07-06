"""Tests for the federation gateway proxy — routing, RBAC, rate limits, health.

Fake downstream connections are injected via GatewayProxy's
``connection_factory`` seam; the end-to-end tests at the bottom use a real
in-memory MCP server (FastMCP over memory streams) to prove actual
protocol round-trips through the gateway.
"""

from __future__ import annotations

import json
import time as _time

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent, Tool

from mcp_org.health import metrics
from mcp_org.middleware.audit import AuditLogger
from mcp_org.middleware.auth import AuthenticatedUser, TeamBasedRBAC
from mcp_org.middleware.rate_limit import RateLimitConfig, RateLimiter
from mcp_org.proxy import (
    DownstreamConnection,
    DownstreamState,
    GatewayProxy,
    SessionDownstream,
)
from mcp_org.registry.servers import DownstreamServerSpec, ServerRegistry

# ─── Fixtures / helpers ──────────────────────────────────────────────────────

SPEC_RUNBOOKS = DownstreamServerSpec(
    name="runbooks", namespace="runbooks", transport="stdio", command="python",
    allowed_teams=("*",),
)
SPEC_DEPLOYS = DownstreamServerSpec(
    name="deploys", namespace="deploy", transport="stdio", command="python",
    allowed_teams=("sre-team",),
)


def make_user(email: str = "alice@co", teams: list[str] | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(email=email, name=email.split("@")[0], teams=teams or ["sre-team"], token="t")


def make_tool(name: str) -> Tool:
    return Tool(name=name, description=f"{name} tool", inputSchema={"type": "object", "properties": {}})


class FakeDownstream(DownstreamConnection):
    """In-memory downstream that records proxied calls."""

    def __init__(self, spec: DownstreamServerSpec, tools: list[Tool] | None = None, responder=None):
        super().__init__(spec)
        self.calls: list[tuple[str, dict, dict | None]] = []
        self._tools = tools or [make_tool("noop")]
        self._responder = responder or (lambda name, args: f"{name} executed")

    async def start(self) -> None:
        self._session = object()  # sentinel: "connected"
        self._mark_healthy()

    async def stop(self) -> None:
        self._session = None
        self._state = DownstreamState.STOPPED

    async def list_tools(self) -> list[Tool]:
        self._tools_cache = list(self._tools)
        return self._tools

    async def call_tool(self, name: str, arguments: dict, meta: dict | None = None) -> CallToolResult:
        self.calls.append((name, arguments, meta))
        response = self._responder(name, arguments)
        if isinstance(response, CallToolResult):
            return response
        return CallToolResult(content=[TextContent(type="text", text=str(response))], isError=False)

    def force_unhealthy(self, error: str = "connection refused") -> None:
        self._session = None
        self._mark_unhealthy(error)


def quiet_audit() -> AuditLogger:
    """Audit logger that discards entries (destination=file with no path)."""
    return AuditLogger(destination="file", log_file=None)


def build_proxy(
    *,
    rbac: TeamBasedRBAC | None = None,
    rate_limiter: RateLimiter | None = None,
    audit: AuditLogger | None = None,
    specs: list[DownstreamServerSpec] | None = None,
    fakes: dict[str, FakeDownstream] | None = None,
) -> tuple[GatewayProxy, dict[str, FakeDownstream]]:
    specs = specs or [SPEC_RUNBOOKS, SPEC_DEPLOYS]
    if fakes is None:
        fakes = {
            "runbooks": FakeDownstream(SPEC_RUNBOOKS, tools=[make_tool("search_runbooks"), make_tool("get_runbook")]),
            "deploys": FakeDownstream(SPEC_DEPLOYS, tools=[make_tool("trigger_deploy")]),
        }
    proxy = GatewayProxy(
        ServerRegistry(specs),
        rbac=rbac or TeamBasedRBAC({"*": ["*"]}),
        rate_limiter=rate_limiter or RateLimiter(config=RateLimitConfig(default_hourly=10_000, default_burst=100)),
        audit=audit or quiet_audit(),
        connection_factory=lambda spec: fakes[spec.name],
    )
    return proxy, fakes


def error_payload(result: CallToolResult) -> dict:
    assert result.isError is True
    assert result.structuredContent is not None
    return result.structuredContent["error"]


# ─── Routing ─────────────────────────────────────────────────────────────────

class TestRouting:
    @pytest.mark.asyncio
    async def test_routes_by_namespace_prefix(self):
        proxy, fakes = build_proxy()
        await proxy.start()

        result = await proxy.dispatch(make_user(), "runbooks__search_runbooks", {"query": "cpu"})

        assert result.isError is False
        assert fakes["runbooks"].calls[0][0] == "search_runbooks"  # prefix stripped
        assert fakes["runbooks"].calls[0][1] == {"query": "cpu"}
        assert fakes["deploys"].calls == []

    @pytest.mark.asyncio
    async def test_routes_to_second_server(self):
        proxy, fakes = build_proxy()
        await proxy.start()

        await proxy.dispatch(make_user(), "deploy__trigger_deploy", {"service": "api"})

        assert fakes["deploys"].calls[0][0] == "trigger_deploy"
        assert fakes["runbooks"].calls == []

    @pytest.mark.asyncio
    async def test_unknown_namespace_returns_structured_error(self):
        proxy, _ = build_proxy()
        await proxy.start()

        result = await proxy.dispatch(make_user(), "ghost__tool", {})

        err = error_payload(result)
        assert err["code"] == "unknown_tool"
        assert "ghost" in err["message"]
        assert "runbooks" in err["message"]  # lists known namespaces

    @pytest.mark.asyncio
    async def test_unprefixed_name_returns_error(self):
        proxy, _ = build_proxy()
        await proxy.start()

        result = await proxy.dispatch(make_user(), "bare_tool", {})
        assert error_payload(result)["code"] == "unknown_tool"

    @pytest.mark.asyncio
    async def test_downstream_result_passthrough(self):
        payload = {"rows": [1, 2, 3]}
        downstream_result = CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structuredContent=payload,
            isError=False,
        )
        fakes = {
            "runbooks": FakeDownstream(SPEC_RUNBOOKS, responder=lambda n, a: downstream_result),
            "deploys": FakeDownstream(SPEC_DEPLOYS),
        }
        proxy, _ = build_proxy(fakes=fakes)
        await proxy.start()

        result = await proxy.dispatch(make_user(), "runbooks__anything", {})
        assert result.structuredContent == payload  # untouched passthrough

    @pytest.mark.asyncio
    async def test_downstream_iserror_passthrough(self):
        bad = CallToolResult(content=[TextContent(type="text", text="boom")], isError=True)
        fakes = {
            "runbooks": FakeDownstream(SPEC_RUNBOOKS, responder=lambda n, a: bad),
            "deploys": FakeDownstream(SPEC_DEPLOYS),
        }
        proxy, _ = build_proxy(fakes=fakes)
        await proxy.start()

        result = await proxy.dispatch(make_user(), "runbooks__x", {})
        assert result.isError is True

    @pytest.mark.asyncio
    async def test_audit_context_propagated_via_meta(self):
        proxy, fakes = build_proxy()
        await proxy.start()

        user = make_user("carol@co", ["sre-team", "security"])
        await proxy.dispatch(user, "runbooks__get_runbook", {"id": "rb-1"})

        meta = fakes["runbooks"].calls[0][2]
        assert meta is not None
        assert meta["principal"] == "carol@co"
        assert meta["teams"] == ["sre-team", "security"]
        assert len(meta["correlation_id"]) == 36  # uuid4
        assert meta["gateway"] == "org-mcp-gateway-proxy"


# ─── Aggregate catalog ───────────────────────────────────────────────────────

class TestAggregateCatalog:
    @pytest.mark.asyncio
    async def test_catalog_is_namespaced_union(self):
        proxy, _ = build_proxy()
        await proxy.start()

        tools = await proxy.aggregate_tools(make_user())
        names = [t.name for t in tools]
        assert names == ["deploy__trigger_deploy", "runbooks__get_runbook", "runbooks__search_runbooks"]
        assert all("[via " in (t.description or "") for t in tools)

    @pytest.mark.asyncio
    async def test_catalog_excludes_unhealthy_server(self):
        proxy, fakes = build_proxy()
        await proxy.start()
        fakes["deploys"].force_unhealthy()

        names = [t.name for t in await proxy.aggregate_tools(make_user())]
        assert names == ["runbooks__get_runbook", "runbooks__search_runbooks"]

    @pytest.mark.asyncio
    async def test_catalog_respects_server_allowed_teams(self):
        proxy, _ = build_proxy()
        await proxy.start()

        dev = make_user("dev@co", ["dev-team"])  # deploys allows only sre-team
        names = [t.name for t in await proxy.aggregate_tools(dev)]
        assert names == ["runbooks__get_runbook", "runbooks__search_runbooks"]

    @pytest.mark.asyncio
    async def test_catalog_respects_tool_level_rbac(self):
        rbac = TeamBasedRBAC({"sre-team": ["runbooks__search_*"]})
        proxy, _ = build_proxy(rbac=rbac)
        await proxy.start()

        names = [t.name for t in await proxy.aggregate_tools(make_user())]
        assert names == ["runbooks__search_runbooks"]

    @pytest.mark.asyncio
    async def test_catalog_empty_for_unauthenticated(self):
        proxy, _ = build_proxy()
        await proxy.start()
        assert await proxy.aggregate_tools(None) == []


# ─── RBAC enforcement through the gateway ────────────────────────────────────

class TestRBACEnforcement:
    @pytest.mark.asyncio
    async def test_server_team_gate_denies(self):
        proxy, fakes = build_proxy()
        await proxy.start()

        dev = make_user("dev@co", ["dev-team"])
        result = await proxy.dispatch(dev, "deploy__trigger_deploy", {})

        err = error_payload(result)
        assert err["code"] == "forbidden"
        assert "deploys" in err["message"]
        assert fakes["deploys"].calls == []  # never reached downstream

    @pytest.mark.asyncio
    async def test_tool_level_rbac_denies(self):
        rbac = TeamBasedRBAC({"sre-team": ["runbooks__*"]})
        proxy, fakes = build_proxy(rbac=rbac)
        await proxy.start()

        result = await proxy.dispatch(make_user(), "deploy__trigger_deploy", {})
        assert error_payload(result)["code"] == "forbidden"
        assert fakes["deploys"].calls == []

    @pytest.mark.asyncio
    async def test_privileged_user_allowed(self):
        rbac = TeamBasedRBAC({"sre-team": ["*"], "dev-team": ["runbooks__*"]})
        proxy, fakes = build_proxy(rbac=rbac)
        await proxy.start()

        result = await proxy.dispatch(make_user(), "deploy__trigger_deploy", {})
        assert result.isError is False
        assert len(fakes["deploys"].calls) == 1

    @pytest.mark.asyncio
    async def test_unauthenticated_call_rejected(self):
        proxy, fakes = build_proxy()
        await proxy.start()

        result = await proxy.dispatch(None, "runbooks__search_runbooks", {})
        assert error_payload(result)["code"] == "unauthenticated"
        assert fakes["runbooks"].calls == []

    @pytest.mark.asyncio
    async def test_denials_are_audited(self, tmp_path):
        audit_file = tmp_path / "audit.jsonl"
        audit = AuditLogger(destination="file", log_file=str(audit_file))
        proxy, _ = build_proxy(audit=audit)
        await proxy.start()

        dev = make_user("dev@co", ["dev-team"])
        await proxy.dispatch(dev, "deploy__trigger_deploy", {"service": "api"})
        await proxy.dispatch(make_user(), "runbooks__search_runbooks", {"q": "x"})

        entries = [json.loads(line) for line in audit_file.read_text().splitlines()]
        assert len(entries) == 2
        denied, allowed = entries
        assert denied["result_summary"] == "DENIED"
        assert denied["success"] is False
        assert denied["user_email"] == "dev@co"
        assert denied["tool_name"] == "deploy__trigger_deploy"
        assert allowed["success"] is True
        assert allowed["tool_name"] == "runbooks__search_runbooks"


# ─── Rate limiting through the gateway ───────────────────────────────────────

class TestRateLimiting:
    def _limited_proxy(self, hourly: int = 2):
        limiter = RateLimiter(config=RateLimitConfig(default_hourly=hourly, default_burst=0))
        return build_proxy(rate_limiter=limiter)

    @pytest.mark.asyncio
    async def test_blocks_after_quota_with_mcp_error(self):
        proxy, fakes = self._limited_proxy(hourly=2)
        await proxy.start()
        user = make_user()

        r1 = await proxy.dispatch(user, "runbooks__search_runbooks", {})
        r2 = await proxy.dispatch(user, "runbooks__search_runbooks", {})
        r3 = await proxy.dispatch(user, "runbooks__search_runbooks", {})

        assert r1.isError is False and r2.isError is False
        err = error_payload(r3)
        assert err["code"] == "rate_limited"
        assert err["retry_after_seconds"] > 0
        assert err["limit"] == 2
        assert len(fakes["runbooks"].calls) == 2  # third call never proxied

    @pytest.mark.asyncio
    async def test_refills_after_time_passes(self, monkeypatch):
        """Advance a mocked monotonic clock one hour: quota fully refills."""
        offset = [0.0]
        real_monotonic = _time.monotonic
        monkeypatch.setattr(
            "mcp_org.middleware.rate_limit.time.monotonic",
            lambda: real_monotonic() + offset[0],
        )
        proxy, _ = self._limited_proxy(hourly=2)
        await proxy.start()
        user = make_user()

        await proxy.dispatch(user, "runbooks__search_runbooks", {})
        await proxy.dispatch(user, "runbooks__search_runbooks", {})
        denied = await proxy.dispatch(user, "runbooks__search_runbooks", {})
        assert denied.isError is True

        offset[0] += 3600.0  # one hour later
        refilled = await proxy.dispatch(user, "runbooks__search_runbooks", {})
        assert refilled.isError is False

    @pytest.mark.asyncio
    async def test_limits_are_per_principal(self):
        proxy, _ = self._limited_proxy(hourly=1)
        await proxy.start()

        assert (await proxy.dispatch(make_user("a@co"), "runbooks__search_runbooks", {})).isError is False
        assert (await proxy.dispatch(make_user("a@co"), "runbooks__search_runbooks", {})).isError is True
        assert (await proxy.dispatch(make_user("b@co"), "runbooks__search_runbooks", {})).isError is False

    @pytest.mark.asyncio
    async def test_limits_are_per_tool(self):
        proxy, _ = self._limited_proxy(hourly=1)
        await proxy.start()
        user = make_user()

        assert (await proxy.dispatch(user, "runbooks__search_runbooks", {})).isError is False
        assert (await proxy.dispatch(user, "runbooks__search_runbooks", {})).isError is True
        assert (await proxy.dispatch(user, "runbooks__get_runbook", {})).isError is False

    @pytest.mark.asyncio
    async def test_denials_increment_metrics_counter(self):
        proxy, _ = self._limited_proxy(hourly=1)
        await proxy.start()
        user = make_user("metrics-user@co")

        before = metrics.get_counter("mcp_gateway_rate_limited_total", tool="runbooks__get_runbook")
        await proxy.dispatch(user, "runbooks__get_runbook", {})
        await proxy.dispatch(user, "runbooks__get_runbook", {})  # denied

        after = metrics.get_counter("mcp_gateway_rate_limited_total", tool="runbooks__get_runbook")
        assert after == before + 1
        assert proxy._rate_limiter.stats()["denied_total"] >= 1


# ─── Downstream health ───────────────────────────────────────────────────────

class TestDownstreamHealth:
    @pytest.mark.asyncio
    async def test_call_to_unhealthy_downstream_rejected(self):
        proxy, fakes = build_proxy()
        await proxy.start()
        fakes["runbooks"].force_unhealthy("process exited")

        result = await proxy.dispatch(make_user(), "runbooks__search_runbooks", {})
        err = error_payload(result)
        assert err["code"] == "downstream_unavailable"
        assert fakes["runbooks"].calls == []

    @pytest.mark.asyncio
    async def test_downstream_exception_becomes_error_result(self):
        def explode(name, args):
            raise ConnectionResetError("pipe broke mid-call")

        fakes = {
            "runbooks": FakeDownstream(SPEC_RUNBOOKS, responder=explode),
            "deploys": FakeDownstream(SPEC_DEPLOYS),
        }
        proxy, _ = build_proxy(fakes=fakes)
        await proxy.start()

        result = await proxy.dispatch(make_user(), "runbooks__search_runbooks", {})
        err = error_payload(result)
        assert err["code"] == "downstream_error"
        assert "pipe broke" in err["message"]
        assert "correlation_id" in err

    @pytest.mark.asyncio
    async def test_health_snapshot_shape(self):
        proxy, fakes = build_proxy()
        await proxy.start()
        fakes["deploys"].force_unhealthy("dns failure")

        health = proxy.downstream_health()
        assert health["runbooks"]["state"] == "healthy"
        assert health["deploys"]["state"] == "unhealthy"
        assert health["deploys"]["last_error"] == "dns failure"
        assert health["deploys"]["consecutive_failures"] == 1

    @pytest.mark.asyncio
    async def test_downstream_up_gauge_tracks_state(self):
        proxy, fakes = build_proxy()
        await proxy.start()
        assert metrics.get_gauge("mcp_gateway_downstream_up", server="runbooks") == 1

        fakes["runbooks"].force_unhealthy()
        assert metrics.get_gauge("mcp_gateway_downstream_up", server="runbooks") == 0


# ─── Lifecycle & registry reconciliation ─────────────────────────────────────

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_disabled_servers_not_connected(self):
        dark_spec = DownstreamServerSpec(
            name="dark", namespace="dark", transport="stdio", command="x", enabled=False
        )
        fakes = {
            "runbooks": FakeDownstream(SPEC_RUNBOOKS),
            "deploys": FakeDownstream(SPEC_DEPLOYS),
            "dark": FakeDownstream(dark_spec),
        }
        proxy, _ = build_proxy(specs=[SPEC_RUNBOOKS, SPEC_DEPLOYS, dark_spec], fakes=fakes)
        await proxy.start()

        assert set(proxy.downstream_health()) == {"runbooks", "deploys"}
        assert fakes["dark"].state == DownstreamState.STOPPED

    @pytest.mark.asyncio
    async def test_registry_change_reconciles_connections(self):
        new_spec = DownstreamServerSpec(name="chaos", namespace="chaos", transport="stdio", command="x")
        fakes = {
            "runbooks": FakeDownstream(SPEC_RUNBOOKS),
            "deploys": FakeDownstream(SPEC_DEPLOYS),
            "chaos": FakeDownstream(new_spec, tools=[make_tool("run_experiment")]),
        }
        proxy, _ = build_proxy(fakes=fakes)
        await proxy.start()
        assert "chaos" not in proxy.downstream_health()

        proxy.registry.register(new_spec)
        await proxy._sync_connections()
        assert proxy.downstream_health()["chaos"]["state"] == "healthy"
        result = await proxy.dispatch(make_user(), "chaos__run_experiment", {})
        assert result.isError is False

        proxy.registry.deregister("chaos")
        await proxy._sync_connections()
        assert "chaos" not in proxy.downstream_health()
        assert fakes["chaos"].state == DownstreamState.STOPPED

    @pytest.mark.asyncio
    async def test_stop_stops_all_downstreams(self):
        proxy, fakes = build_proxy()
        await proxy.start()
        await proxy.stop()
        assert fakes["runbooks"].state == DownstreamState.STOPPED
        assert fakes["deploys"].state == DownstreamState.STOPPED
        assert proxy.downstream_health() == {}


# ─── Config loading ──────────────────────────────────────────────────────────

class TestFromConfig:
    def test_from_config_builds_gateway(self, tmp_path):
        config = tmp_path / "gateway.yaml"
        config.write_text(
            """
gateway:
  name: test-gateway
  namespace_separator: "__"
  hot_reload_interval_seconds: 5
rate_limits:
  default_hourly: 42
  default_burst: 7
rbac_rules:
  sre-team: ["*"]
  dev-team: ["runbooks__*"]
servers:
  - name: runbooks
    namespace: runbooks
    transport: stdio
    command: python
"""
        )
        proxy = GatewayProxy.from_config(config)
        assert proxy.server.name == "test-gateway"
        assert len(proxy.registry) == 1
        assert proxy._rate_limiter._config.default_hourly == 42
        assert proxy._rbac._rules["dev-team"] == ["runbooks__*"]
        assert proxy._hot_reload_interval == 5.0

    def test_from_config_rejects_bad_extension(self, tmp_path):
        config = tmp_path / "gateway.ini"
        config.write_text("[gateway]")
        with pytest.raises(ValueError, match="Unsupported config format"):
            GatewayProxy.from_config(config)

    def test_from_config_invalid_servers_raise(self, tmp_path):
        config = tmp_path / "gateway.yaml"
        config.write_text("servers:\n  - name: bad\n    namespace: bad\n    transport: sse\n")
        with pytest.raises(Exception, match="requires 'url'"):
            GatewayProxy.from_config(config)


# ─── End-to-end over real MCP protocol (in-memory) ───────────────────────────

def _echo_downstream() -> FastMCP:
    server = FastMCP("echo-downstream")

    @server.tool()
    def echo(text: str) -> str:
        """Echo the input back."""
        return f"echo: {text}"

    return server


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_dispatch_through_real_mcp_session(self):
        """Gateway → real ClientSession → real MCP server over memory streams."""
        downstream = _echo_downstream()
        async with create_connected_server_and_client_session(downstream._mcp_server) as session:
            conn = SessionDownstream(SPEC_RUNBOOKS, session)
            proxy = GatewayProxy(
                ServerRegistry([SPEC_RUNBOOKS]),
                rbac=TeamBasedRBAC({"*": ["*"]}),
                rate_limiter=RateLimiter(config=RateLimitConfig(default_hourly=100, default_burst=10)),
                audit=quiet_audit(),
                connection_factory=lambda spec: conn,
            )
            await proxy.start()
            try:
                tools = await proxy.aggregate_tools(make_user())
                assert [t.name for t in tools] == ["runbooks__echo"]

                result = await proxy.dispatch(make_user(), "runbooks__echo", {"text": "hello"})
                assert result.isError is False
                assert "echo: hello" in result.content[0].text
            finally:
                await proxy.stop()

    @pytest.mark.asyncio
    async def test_full_chain_client_to_gateway_to_downstream(self):
        """MCP client → gateway MCP server → downstream MCP server, all real protocol."""
        downstream = _echo_downstream()
        async with create_connected_server_and_client_session(downstream._mcp_server) as session:
            conn = SessionDownstream(SPEC_RUNBOOKS, session)
            proxy = GatewayProxy(
                ServerRegistry([SPEC_RUNBOOKS]),
                rbac=TeamBasedRBAC({"*": ["*"]}),
                rate_limiter=RateLimiter(config=RateLimitConfig(default_hourly=100, default_burst=10)),
                audit=quiet_audit(),
                connection_factory=lambda spec: conn,
            )
            proxy.set_default_user(make_user())
            await proxy.start()
            try:
                async with create_connected_server_and_client_session(proxy.server) as gw_client:
                    catalog = await gw_client.list_tools()
                    assert "runbooks__echo" in [t.name for t in catalog.tools]

                    result = await gw_client.call_tool("runbooks__echo", {"text": "round trip"})
                    assert result.isError is False
                    assert "echo: round trip" in result.content[0].text

                    denied = await gw_client.call_tool("ghost__tool", {})
                    assert denied.isError is True
            finally:
                await proxy.stop()
