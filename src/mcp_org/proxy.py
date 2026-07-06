"""Federation gateway — a front-door MCP server that multiplexes downstream MCP servers.

Clients connect to *one* MCP endpoint (this gateway). The gateway:

  - routes tool calls to downstream MCP servers by namespace prefix
    (``runbooks__search_runbooks`` → server owning namespace ``runbooks``,
    local tool ``search_runbooks``)
  - applies authentication + team RBAC per route (server-level
    ``allowed_teams`` gate, then tool-level RBAC rules)
  - enforces per-principal per-tool token-bucket rate limits
  - writes an audit entry for every decision (allowed, denied, limited)
  - propagates audit context (correlation ID, principal, teams) to the
    downstream server via MCP request ``_meta``
  - health-checks downstreams with periodic pings, reconnecting with
    exponential backoff, and excludes unhealthy servers from the catalog
  - surfaces an aggregate tool catalog with namespaced tool names

Topology::

    Claude Desktop / IDE / Slack bot
              │  (stdio / SSE)
              ▼
    ┌───────────────────────────┐
    │       GatewayProxy        │   auth → RBAC → rate limit → audit
    └──────┬──────┬──────┬──────┘
           │      │      │            (stdio subprocess / SSE / streamable HTTP)
           ▼      ▼      ▼
       runbooks  deploy  incidents    ← downstream MCP servers (ServerRegistry)

The namespace separator defaults to ``__`` because most MCP clients
restrict tool names to ``[a-zA-Z0-9_-]``.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from mcp_org.health import metrics
from mcp_org.middleware.audit import AuditLogger
from mcp_org.middleware.auth import (
    APIKeyAuthMiddleware,
    AuthenticatedUser,
    OIDCAuthMiddleware,
    TeamBasedRBAC,
)
from mcp_org.middleware.correlation import bind_correlation_id
from mcp_org.middleware.rate_limit import RateLimitConfig, RateLimiter
from mcp_org.registry.servers import DownstreamServerSpec, ServerRegistry

log = structlog.get_logger()

DEFAULT_NAMESPACE_SEPARATOR = "__"

# Machine-readable error codes surfaced in gateway error results
ERR_UNAUTHENTICATED = "unauthenticated"
ERR_FORBIDDEN = "forbidden"
ERR_RATE_LIMITED = "rate_limited"
ERR_UNKNOWN_TOOL = "unknown_tool"
ERR_DOWNSTREAM_UNAVAILABLE = "downstream_unavailable"
ERR_DOWNSTREAM_ERROR = "downstream_error"

# Per-request principal (mirrors the contextvar pattern in gateway.py)
_current_user: contextvars.ContextVar[AuthenticatedUser | None] = contextvars.ContextVar(
    "proxy_current_user", default=None
)
_current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "proxy_current_session", default=None
)


class DownstreamState(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class DownstreamUnavailableError(RuntimeError):
    """Raised when a call targets a downstream that has no live session."""


class DownstreamConnection:
    """Base class managing one downstream MCP session.

    Subclasses own the transport lifecycle. ``SessionDownstream`` wraps an
    externally managed session (embedding, tests); ``TransportDownstream``
    supervises a real stdio/SSE/streamable-HTTP connection with reconnects.
    """

    def __init__(self, spec: DownstreamServerSpec) -> None:
        self.spec = spec
        self._session: ClientSession | None = None
        self._state: DownstreamState = DownstreamState.STOPPED
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._last_healthy_at: float | None = None
        self._tools_cache: list[Tool] = []

    # -- lifecycle (overridden by subclasses) ---------------------------------

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    # -- state ----------------------------------------------------------------

    @property
    def state(self) -> DownstreamState:
        return self._state

    @property
    def is_healthy(self) -> bool:
        return self._state == DownstreamState.HEALTHY and self._session is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "server": self.spec.name,
            "namespace": self.spec.namespace,
            "transport": self.spec.transport,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            "last_healthy_at": self._last_healthy_at,
            "cached_tools": len(self._tools_cache),
        }

    def _mark_healthy(self) -> None:
        self._state = DownstreamState.HEALTHY
        self._consecutive_failures = 0
        self._last_error = None
        self._last_healthy_at = time.time()
        metrics.set_gauge(
            "mcp_gateway_downstream_up", 1,
            help_text="Downstream MCP server connectivity (1=healthy)",
            server=self.spec.name,
        )

    def _mark_unhealthy(self, error: str) -> None:
        self._state = DownstreamState.UNHEALTHY
        self._consecutive_failures += 1
        self._last_error = error
        metrics.set_gauge(
            "mcp_gateway_downstream_up", 0,
            help_text="Downstream MCP server connectivity (1=healthy)",
            server=self.spec.name,
        )

    # -- MCP operations ---------------------------------------------------------

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise DownstreamUnavailableError(
                f"Downstream server '{self.spec.name}' is {self._state.value}"
                + (f" (last error: {self._last_error})" if self._last_error else "")
            )
        return self._session

    async def ping(self) -> bool:
        """Health-check the downstream. Returns True if it responded in time."""
        try:
            session = self._require_session()
            await asyncio.wait_for(session.send_ping(), timeout=self.spec.ping_timeout_seconds)
            return True
        except Exception as exc:
            self._mark_unhealthy(f"ping failed: {exc}")
            return False

    async def list_tools(self) -> list[Tool]:
        """Fetch the downstream tool list, falling back to the last-known cache."""
        try:
            session = self._require_session()
            result = await asyncio.wait_for(
                session.list_tools(), timeout=self.spec.request_timeout_seconds
            )
            self._tools_cache = list(result.tools)
            return self._tools_cache
        except DownstreamUnavailableError:
            return list(self._tools_cache)
        except Exception as exc:
            log.warning("downstream.list_tools_failed", server=self.spec.name, error=str(exc))
            return list(self._tools_cache)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Proxy one tool call to the downstream server."""
        session = self._require_session()
        return await session.call_tool(
            name,
            arguments,
            read_timeout_seconds=timedelta(seconds=self.spec.request_timeout_seconds),
            meta=meta,
        )


class SessionDownstream(DownstreamConnection):
    """Wraps an externally managed ClientSession (in-memory embedding, tests)."""

    def __init__(self, spec: DownstreamServerSpec, session: ClientSession) -> None:
        super().__init__(spec)
        self._session = session

    async def start(self) -> None:
        self._mark_healthy()

    async def stop(self) -> None:
        self._session = None
        self._state = DownstreamState.STOPPED


class TransportDownstream(DownstreamConnection):
    """Supervised connection over stdio / SSE / streamable HTTP.

    A background task owns the transport context managers, initializes the
    MCP session, then runs a ping loop at ``health_check_interval_seconds``.
    Any failure tears the session down and reconnects with exponential
    backoff (1s → 60s). Requests from other tasks go through the shared
    ``ClientSession``, which multiplexes safely by request ID.
    """

    RECONNECT_INITIAL_SECONDS = 1.0
    RECONNECT_MAX_SECONDS = 60.0

    def __init__(self, spec: DownstreamServerSpec) -> None:
        super().__init__(spec)
        self._supervisor: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()
        self._ready = asyncio.Event()

    async def start(self) -> None:
        """Start the supervisor and wait (bounded) for the first connection.

        Never raises on connection failure — the supervisor keeps retrying
        in the background so one dead downstream cannot block gateway boot.
        """
        if self._supervisor is not None and not self._supervisor.done():
            return
        self._shutdown = asyncio.Event()
        self._ready = asyncio.Event()
        self._state = DownstreamState.CONNECTING
        self._supervisor = asyncio.create_task(
            self._run(), name=f"downstream-{self.spec.name}"
        )
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.spec.connect_timeout_seconds)
        except TimeoutError:
            log.warning(
                "downstream.slow_start",
                server=self.spec.name,
                waited_seconds=self.spec.connect_timeout_seconds,
                note="continuing to retry in background",
            )

    async def stop(self) -> None:
        self._shutdown.set()
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor = None
        self._session = None
        self._state = DownstreamState.STOPPED
        log.info("downstream.stopped", server=self.spec.name)

    @asynccontextmanager
    async def _open_transport(self):
        spec = self.spec
        if spec.transport == "stdio":
            env = get_default_environment()
            env.update({k: str(v) for k, v in spec.env.items()})
            params = StdioServerParameters(
                command=spec.command or "",
                args=list(spec.args),
                env=env,
                cwd=spec.cwd,
            )
            async with stdio_client(params) as (read, write):
                yield read, write
        elif spec.transport == "sse":
            async with sse_client(
                spec.url or "", headers=dict(spec.headers) or None
            ) as (read, write):
                yield read, write
        elif spec.transport == "streamable-http":
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(
                spec.url or "", headers=dict(spec.headers) or None
            ) as (read, write, _get_session_id):
                yield read, write
        else:  # pragma: no cover — registry validation prevents this
            raise ValueError(f"Unsupported transport: {spec.transport}")

    async def _run(self) -> None:
        backoff = self.RECONNECT_INITIAL_SECONDS
        while not self._shutdown.is_set():
            try:
                async with self._open_transport() as (read, write):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(
                            session.initialize(), timeout=self.spec.connect_timeout_seconds
                        )
                        self._session = session
                        self._mark_healthy()
                        self._ready.set()
                        backoff = self.RECONNECT_INITIAL_SECONDS
                        log.info(
                            "downstream.connected",
                            server=self.spec.name,
                            transport=self.spec.transport,
                        )
                        # Prime the tool catalog cache
                        await self.list_tools()
                        await self._health_loop(session)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._mark_unhealthy(str(exc))
                log.warning(
                    "downstream.connection_lost",
                    server=self.spec.name,
                    error=str(exc),
                    retry_in_seconds=backoff,
                    consecutive_failures=self._consecutive_failures,
                )
            finally:
                self._session = None

            if self._shutdown.is_set():
                break
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self.RECONNECT_MAX_SECONDS)

    async def _health_loop(self, session: ClientSession) -> None:
        """Ping at the configured interval until shutdown or a failed ping."""
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self.spec.health_check_interval_seconds
                )
                return  # shutdown requested
            except TimeoutError:
                pass
            try:
                await asyncio.wait_for(session.send_ping(), timeout=self.spec.ping_timeout_seconds)
                self._mark_healthy()
                # Refresh the tool catalog opportunistically after a good ping
                await self.list_tools()
            except Exception as exc:
                raise ConnectionError(f"health check failed: {exc}") from exc


class GatewayProxy:
    """Front-door MCP server multiplexing the downstream fleet."""

    def __init__(
        self,
        registry: ServerRegistry,
        *,
        auth: APIKeyAuthMiddleware | OIDCAuthMiddleware | None = None,
        rbac: TeamBasedRBAC | None = None,
        rate_limiter: RateLimiter | None = None,
        audit: AuditLogger | None = None,
        namespace_separator: str = DEFAULT_NAMESPACE_SEPARATOR,
        connection_factory: type[DownstreamConnection] | Any = TransportDownstream,
        hot_reload_interval_seconds: float = 30.0,
        name: str = "org-mcp-gateway-proxy",
    ) -> None:
        self._registry = registry
        self._auth = auth or _auth_from_env()
        self._rbac = rbac or TeamBasedRBAC({"*": ["*"]})
        self._rate_limiter = rate_limiter or RateLimiter()
        self._audit = audit or AuditLogger(
            destination=os.environ.get("AUDIT_DESTINATION", "stdout"),
            s3_bucket=os.environ.get("MCP_AUDIT_S3_BUCKET"),
        )
        self._separator = namespace_separator
        self._connection_factory = connection_factory
        self._hot_reload_interval = hot_reload_interval_seconds
        self._connections: dict[str, DownstreamConnection] = {}
        self._default_user: AuthenticatedUser | None = None
        self._hot_reload_task: asyncio.Task[None] | None = None
        self._started = False

        self._app = Server(name)
        self._register_handlers()

    # ------------------------------------------------------------------
    # MCP handlers
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        app = self._app

        @app.list_tools()
        async def _list_tools() -> list[Tool]:
            return await self.aggregate_tools(self._resolve_user())

        # validate_input=False: the downstream server owns the schema and
        # validates on its side; the gateway catalog may lag a hot deploy.
        @app.call_tool(validate_input=False)
        async def _call_tool(name: str, arguments: dict) -> CallToolResult:
            return await self.dispatch(self._resolve_user(), name, arguments or {})

    def _resolve_user(self) -> AuthenticatedUser | None:
        return _current_user.get() or self._default_user

    def set_default_user(self, user: AuthenticatedUser | None) -> None:
        """Set the fallback principal (stdio single-user mode)."""
        self._default_user = user

    @property
    def server(self) -> Server:
        """The underlying MCP Server (for embedding in custom transports)."""
        return self._app

    @property
    def registry(self) -> ServerRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect all enabled downstreams and begin hot-reload polling."""
        if self._started:
            return
        self._started = True
        await self._sync_connections()
        if self._registry_has_source():
            self._hot_reload_task = asyncio.create_task(
                self._hot_reload_loop(), name="gateway-hot-reload"
            )
        catalog_size = sum(len(c._tools_cache) for c in self._connections.values())
        metrics.set_ready(True, tool_count=catalog_size)
        log.info(
            "gateway_proxy.started",
            downstreams=len(self._connections),
            healthy=[c.spec.name for c in self._connections.values() if c.is_healthy],
        )

    async def stop(self) -> None:
        if self._hot_reload_task is not None:
            self._hot_reload_task.cancel()
            try:
                await self._hot_reload_task
            except (asyncio.CancelledError, Exception):
                pass
            self._hot_reload_task = None
        await asyncio.gather(*(conn.stop() for conn in self._connections.values()), return_exceptions=True)
        self._connections.clear()
        self._started = False
        metrics.set_ready(False)
        log.info("gateway_proxy.stopped")

    def _registry_has_source(self) -> bool:
        return getattr(self._registry, "_source_path", None) is not None

    async def _sync_connections(self) -> None:
        """Reconcile live connections with the registry's enabled spec set."""
        desired = {s.name: s for s in self._registry.list_servers(enabled_only=True)}

        # Stop connections that were removed, disabled, or whose spec changed
        for name in list(self._connections):
            conn = self._connections[name]
            if name not in desired or desired[name] != conn.spec:
                await conn.stop()
                del self._connections[name]

        # Start connections for new or changed specs
        for name, spec in desired.items():
            if name not in self._connections:
                conn = self._connection_factory(spec)
                self._connections[name] = conn
                try:
                    await conn.start()
                except Exception as exc:
                    # Defensive: factory-provided connections may raise on start
                    conn._mark_unhealthy(str(exc))
                    log.error("downstream.start_failed", server=name, error=str(exc))

    async def _hot_reload_loop(self) -> None:
        while True:
            await asyncio.sleep(self._hot_reload_interval)
            try:
                if await asyncio.to_thread(self._registry.maybe_reload):
                    log.info("gateway_proxy.registry_changed", note="reconciling downstream connections")
                    await self._sync_connections()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("gateway_proxy.hot_reload_error", error=str(exc))

    def downstream_health(self) -> dict[str, dict[str, Any]]:
        """Per-downstream health snapshot (for /ready integration and admin)."""
        return {name: conn.health_snapshot() for name, conn in sorted(self._connections.items())}

    # ------------------------------------------------------------------
    # Aggregate catalog
    # ------------------------------------------------------------------

    async def aggregate_tools(self, user: AuthenticatedUser | None) -> list[Tool]:
        """Namespaced union of downstream catalogs, filtered by RBAC for *user*."""
        if user is None:
            return []

        catalog: list[Tool] = []
        for conn in self._connections.values():
            spec = conn.spec
            if not spec.allows_teams(user.teams):
                continue
            if not conn.is_healthy:
                continue
            try:
                tools = await conn.list_tools()
            except Exception as exc:
                log.warning("gateway_proxy.catalog_fetch_failed", server=spec.name, error=str(exc))
                continue
            for tool in tools:
                full_name = f"{spec.namespace}{self._separator}{tool.name}"
                if not self._rbac.can_call(user, full_name):
                    continue
                description = tool.description or ""
                origin = f"[via {spec.name}]"
                catalog.append(
                    tool.model_copy(
                        update={
                            "name": full_name,
                            "description": f"{description} {origin}".strip(),
                        }
                    )
                )
        catalog.sort(key=lambda t: t.name)
        return catalog

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        user: AuthenticatedUser | None,
        full_name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        """Route one namespaced tool call through auth → RBAC → rate limit → downstream."""
        start = time.monotonic()
        session_id = _current_session.get() or "stdio-session"
        correlation_id = bind_correlation_id()
        log.info("gateway_proxy.call", tool=full_name, correlation_id=correlation_id)

        if user is None:
            return _error_result(
                ERR_UNAUTHENTICATED,
                "Unauthenticated — provide a valid credential (Authorization header or ORG_MCP_TOKEN)",
            )

        # Resolve route
        try:
            spec, local_name = self._registry.resolve_tool(full_name, separator=self._separator)
        except KeyError as exc:
            metrics.inc_counter(
                "mcp_gateway_unknown_tool_total",
                help_text="Calls to tools with no matching downstream namespace",
                tool=full_name,
            )
            return _error_result(ERR_UNKNOWN_TOOL, str(exc.args[0]) if exc.args else str(exc))

        def audit(result: str, success: bool, error: str | None = None) -> None:
            self._audit.log_call(
                session_id=session_id,
                user_email=user.email,
                user_teams=user.teams,
                tool_name=full_name,
                tool_args=arguments,
                result=result,
                duration_ms=int((time.monotonic() - start) * 1000),
                success=success,
                error=error,
            )

        # Server-level team gate
        if not spec.allows_teams(user.teams):
            metrics.inc_counter(
                "mcp_gateway_rbac_denied_total",
                help_text="Tool calls denied by RBAC",
                tool=full_name, reason="server_teams",
            )
            message = (
                f"Access denied: teams {list(user.teams)} may not use server '{spec.name}' "
                f"(allowed: {list(spec.allowed_teams)})"
            )
            audit("DENIED", False, message)
            return _error_result(ERR_FORBIDDEN, message)

        # Tool-level RBAC
        if not self._rbac.can_call(user, full_name):
            metrics.inc_counter(
                "mcp_gateway_rbac_denied_total",
                help_text="Tool calls denied by RBAC",
                tool=full_name, reason="tool_rules",
            )
            message = f"Access denied: {user.email} is not authorized to call {full_name}"
            audit("DENIED", False, message)
            return _error_result(ERR_FORBIDDEN, message)

        # Rate limit (per principal, per namespaced tool)
        rl = self._rate_limiter.check(user.email, full_name, user.teams)
        if not rl.allowed:
            metrics.inc_counter(
                "mcp_gateway_rate_limited_total",
                help_text="Tool calls rejected by the rate limiter",
                tool=full_name,
            )
            message = (
                f"Rate limit exceeded for {user.email} on '{full_name}'. "
                f"Retry after {rl.retry_after_seconds:.1f}s (limit {rl.limit}/hour)."
            )
            audit("RATE_LIMITED", False, message)
            return _error_result(
                ERR_RATE_LIMITED,
                message,
                retry_after_seconds=round(rl.retry_after_seconds, 3),
                limit=rl.limit,
                remaining=rl.remaining_tokens,
                reset_at=rl.reset_at,
            )

        # Downstream availability
        conn = self._connections.get(spec.name)
        if conn is None or not conn.is_healthy:
            snapshot = conn.health_snapshot() if conn else {"state": "not_connected"}
            message = (
                f"Downstream server '{spec.name}' is unavailable "
                f"(state: {snapshot.get('state')}). Try again shortly."
            )
            audit("DOWNSTREAM_UNAVAILABLE", False, message)
            metrics.inc_counter(
                "mcp_gateway_downstream_unavailable_total",
                help_text="Calls rejected because the downstream was unhealthy",
                server=spec.name,
            )
            return _error_result(ERR_DOWNSTREAM_UNAVAILABLE, message, server=spec.name)

        # Proxy the call, propagating audit context via MCP _meta
        meta = {
            "correlation_id": correlation_id,
            "principal": user.email,
            "teams": list(user.teams),
            "gateway": self._app.name,
            "session_id": session_id,
        }
        try:
            result = await conn.call_tool(local_name, arguments, meta=meta)
        except Exception as exc:  # McpError, DownstreamUnavailableError, timeout, transport loss
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_call(full_name, duration_ms, success=False)
            metrics.inc_counter(
                "mcp_gateway_proxied_errors_total",
                help_text="Proxied calls that failed at the downstream",
                server=spec.name,
            )
            audit("", False, str(exc))
            log.error(
                "gateway_proxy.downstream_call_failed",
                server=spec.name, tool=local_name, error=str(exc),
            )
            return _error_result(
                ERR_DOWNSTREAM_ERROR,
                f"Downstream server '{spec.name}' failed while executing '{local_name}': {exc}",
                server=spec.name,
                correlation_id=correlation_id,
            )

        duration_ms = (time.monotonic() - start) * 1000
        metrics.record_call(full_name, duration_ms, success=not result.isError)
        metrics.inc_counter(
            "mcp_gateway_proxied_calls_total",
            help_text="Tool calls proxied to downstream servers",
            server=spec.name,
        )
        audit(
            _summarize_result(result),
            success=not result.isError,
            error=_summarize_result(result) if result.isError else None,
        )
        return result

    # ------------------------------------------------------------------
    # Entrypoints
    # ------------------------------------------------------------------

    async def run_stdio(self) -> None:
        """Run the gateway as a stdio MCP server (single-principal mode).

        The principal is authenticated once from ORG_MCP_TOKEN, matching the
        behaviour of the single-server gateway in gateway.py.
        """
        token = os.environ.get("ORG_MCP_TOKEN", "")
        user = await self._auth.authenticate(token)
        if user is None:
            log.warning("gateway_proxy.unauthenticated_stdio", note="all tool calls will be rejected")
        self.set_default_user(user)
        _current_session.set("stdio-session")

        await self.start()
        try:
            async with stdio_server() as (read_stream, write_stream):
                await self._app.run(
                    read_stream, write_stream, self._app.create_initialization_options()
                )
        finally:
            await self.stop()

    @classmethod
    def from_config(cls, path: str | Path, **overrides: Any) -> GatewayProxy:
        """Build a gateway from a single YAML/JSON config file.

        Recognised top-level keys:
          - ``servers``      (required) — downstream server list (see ServerRegistry)
          - ``rate_limits``  (optional) — RateLimitConfig fields
          - ``rbac_rules``   (optional) — team → tool-pattern rules
          - ``gateway``      (optional) — name, namespace_separator,
                              hot_reload_interval_seconds
        """
        p = Path(path)
        suffix = p.suffix.lower()
        text = p.read_text(encoding="utf-8")
        if suffix in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(text)
        elif suffix == ".json":
            data = json.loads(text)
        else:
            raise ValueError(f"Unsupported config format {suffix!r} (use .yaml, .yml, or .json)")
        if not isinstance(data, Mapping):
            raise ValueError("Gateway config must be a mapping with a 'servers' key")

        registry = ServerRegistry.from_file(p)

        rate_limiter = None
        if isinstance(data.get("rate_limits"), Mapping):
            rate_limiter = RateLimiter(config=RateLimitConfig.from_dict(dict(data["rate_limits"])))

        rbac = None
        if isinstance(data.get("rbac_rules"), Mapping):
            rbac = TeamBasedRBAC({str(k): list(v) for k, v in data["rbac_rules"].items()})

        gw = data.get("gateway") if isinstance(data.get("gateway"), Mapping) else {}
        kwargs: dict[str, Any] = {
            "rate_limiter": rate_limiter,
            "rbac": rbac,
            "namespace_separator": gw.get("namespace_separator", DEFAULT_NAMESPACE_SEPARATOR),
            "hot_reload_interval_seconds": float(gw.get("hot_reload_interval_seconds", 30.0)),
            "name": gw.get("name", "org-mcp-gateway-proxy"),
        }
        kwargs.update(overrides)
        return cls(registry, **{k: v for k, v in kwargs.items() if v is not None})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_from_env() -> APIKeyAuthMiddleware | OIDCAuthMiddleware:
    if os.environ.get("AUTH_MODE", "apikey") == "oidc":
        return OIDCAuthMiddleware(
            issuer=os.environ["OIDC_ISSUER"],
            client_id=os.environ["OIDC_CLIENT_ID"],
        )
    return APIKeyAuthMiddleware()


def _error_result(code: str, message: str, **data: Any) -> CallToolResult:
    """Build a structured MCP tool error result."""
    payload = {"error": {"code": code, "message": message, **data}}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
        structuredContent=payload,
        isError=True,
    )


def _summarize_result(result: CallToolResult, limit: int = 200) -> str:
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(f"<{getattr(block, 'type', 'content')}>")
        if sum(len(x) for x in parts) >= limit:
            break
    return " ".join(parts)[:limit]


def main() -> None:
    """CLI entrypoint: MCP_GATEWAY_CONFIG (or argv[1]) points at gateway.yaml."""
    import logging
    import sys

    logging.basicConfig(level=logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ]
    )
    config_path = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MCP_GATEWAY_CONFIG", "gateway.yaml")
    )
    proxy = GatewayProxy.from_config(config_path)
    asyncio.run(proxy.run_stdio())


if __name__ == "__main__":
    main()
