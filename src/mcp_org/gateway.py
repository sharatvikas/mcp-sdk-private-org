"""Enterprise MCP Gateway — multi-tenant MCP server with auth, RBAC, and audit logging."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent

from mcp_org.middleware.auth import APIKeyAuthMiddleware, OIDCAuthMiddleware, TeamBasedRBAC, AuthenticatedUser
from mcp_org.middleware.audit import AuditLogger
from mcp_org.middleware.rate_limit import RateLimiter
from mcp_org.registry.loader import ToolRegistry

log = structlog.get_logger()


class OrgMCPGateway:
    """
    Production-grade MCP gateway for internal org use.

    Features:
    - Authentication: OIDC or API key
    - Authorization: Team-based RBAC per tool
    - Audit logging: every call logged to stdout/file/S3/CloudWatch
    - Rate limiting: per-tenant limits
    - Dynamic tool loading: add tools without restart
    """

    def __init__(self) -> None:
        self._setup_auth()
        self._setup_rbac()
        self._registry = ToolRegistry()
        self._audit = AuditLogger(
            destination=os.environ.get("AUDIT_DESTINATION", "stdout"),
            s3_bucket=os.environ.get("MCP_AUDIT_S3_BUCKET"),
        )
        self._rate_limiter = RateLimiter()
        self._app = Server("org-mcp-gateway")
        self._register_handlers()

    def _setup_auth(self) -> None:
        auth_mode = os.environ.get("AUTH_MODE", "apikey")
        if auth_mode == "oidc":
            self._auth = OIDCAuthMiddleware(
                issuer=os.environ["OIDC_ISSUER"],
                client_id=os.environ["OIDC_CLIENT_ID"],
            )
        else:
            self._auth = APIKeyAuthMiddleware()

    def _setup_rbac(self) -> None:
        import json
        rules_json = os.environ.get("RBAC_RULES", "{}")
        try:
            rules = json.loads(rules_json)
        except Exception:
            # Default: SRE team gets everything, everyone else gets read-only
            rules = {
                "sre-team": ["*"],
                "dev-team": ["get_*", "search_*", "list_*", "describe_*"],
                "security": ["*"],
                "*": ["get_oncall_schedule", "search_runbooks"],
            }
        self._rbac = TeamBasedRBAC(rules)

    def _register_handlers(self) -> None:
        app = self._app

        @app.list_tools()
        async def list_tools() -> list[Tool]:
            user = _current_user.get()
            all_tools = await self._registry.get_all_tools()
            if user:
                return [t for t in all_tools if self._rbac.can_call(user, t.name)]
            return []

        @app.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            user = _current_user.get()
            session_id = _current_session.get() or "unknown"
            start = time.monotonic()

            # Auth check
            if not user:
                raise PermissionError("Unauthenticated — provide Authorization header")

            # RBAC check
            if not self._rbac.can_call(user, name):
                self._audit.log_call(
                    session_id=session_id,
                    user_email=user.email,
                    user_teams=user.teams,
                    tool_name=name,
                    tool_args=arguments,
                    result="DENIED",
                    duration_ms=0,
                    success=False,
                    error=f"User {user.email} not authorized to call {name}",
                )
                raise PermissionError(f"Access denied: {user.email} cannot call {name}")

            # Rate limit check
            rl_result = self._rate_limiter.check(user.email, name, user.teams)
            if not rl_result.allowed:
                self._audit.log_call(
                    session_id=session_id,
                    user_email=user.email,
                    user_teams=user.teams,
                    tool_name=name,
                    tool_args=arguments,
                    result="RATE_LIMITED",
                    duration_ms=0,
                    success=False,
                    error=(
                        f"Rate limit exceeded: {rl_result.remaining_tokens} tokens left, "
                        f"retry in {rl_result.retry_after_seconds:.1f}s"
                    ),
                )
                raise RuntimeError(
                    f"Rate limit exceeded for {user.email} on tool '{name}'. "
                    f"Retry after {rl_result.retry_after_seconds:.1f}s. "
                    f"Limit: {rl_result.limit}/hour."
                )

            # Execute tool
            try:
                result = await self._registry.call_tool(name, arguments)
                duration_ms = int((time.monotonic() - start) * 1000)
                self._audit.log_call(
                    session_id=session_id,
                    user_email=user.email,
                    user_teams=user.teams,
                    tool_name=name,
                    tool_args=arguments,
                    result=result[:200] if result else "",
                    duration_ms=duration_ms,
                    success=True,
                )
                return [TextContent(type="text", text=result)]
            except Exception as e:
                duration_ms = int((time.monotonic() - start) * 1000)
                self._audit.log_call(
                    session_id=session_id,
                    user_email=user.email,
                    user_teams=user.teams,
                    tool_name=name,
                    tool_args=arguments,
                    result="",
                    duration_ms=duration_ms,
                    success=False,
                    error=str(e),
                )
                raise

    async def run_stdio(self) -> None:
        """Run as stdio MCP server (for Claude Desktop single-user)."""
        # In stdio mode, use API key from env as the user
        token = os.environ.get("ORG_MCP_TOKEN", "")
        user = await self._auth.authenticate(token)
        _current_user.set(user)
        _current_session.set("stdio-session")

        async with stdio_server() as (read_stream, write_stream):
            await self._app.run(read_stream, write_stream, self._app.create_initialization_options())


# Context vars for per-request user identity (thread-safe)
import contextvars
_current_user: contextvars.ContextVar[AuthenticatedUser | None] = contextvars.ContextVar("current_user", default=None)
_current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_session", default=None)


def main() -> None:
    import logging
    logging.basicConfig(level=logging.WARNING)
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ]
    )
    gateway = OrgMCPGateway()
    asyncio.run(gateway.run_stdio())


if __name__ == "__main__":
    main()
