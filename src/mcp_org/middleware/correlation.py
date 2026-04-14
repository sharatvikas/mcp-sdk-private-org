"""Request correlation ID middleware for the Enterprise MCP Gateway.

Every MCP tool call gets a unique correlation ID that:
- Appears in every structured log line for that call (via structlog context)
- Is included in the audit log entry
- Can be injected into downstream HTTP calls (X-Correlation-ID header)
- Is returned to the caller in the tool result for traceability

Usage::

    from mcp_org.middleware.correlation import correlation_id, bind_correlation_id

    # Inside a request handler:
    bind_correlation_id()          # generates new UUID, binds to structlog + contextvars
    cid = correlation_id.get()    # read it back anywhere in the call stack
    log.info("doing work")        # automatically includes correlation_id in log line
"""

from __future__ import annotations

import contextvars
import uuid

import structlog

# Context var — survives asyncio task switches within the same task tree
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def bind_correlation_id(cid: str | None = None) -> str:
    """Generate (or accept) a correlation ID and bind it to the current context.

    Args:
        cid: Existing correlation ID to propagate (e.g. from X-Correlation-ID header).
             If None, a new UUID4 is generated.

    Returns:
        The correlation ID that was bound.
    """
    if cid is None:
        cid = str(uuid.uuid4())
    correlation_id.set(cid)
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    return cid


def clear_correlation_id() -> None:
    """Remove the correlation ID from the current context and structlog."""
    correlation_id.set("")
    structlog.contextvars.unbind_contextvars("correlation_id")


def get_correlation_id() -> str:
    """Return the current correlation ID, or an empty string if not set."""
    return correlation_id.get()


def inject_into_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return *headers* with X-Correlation-ID added (for downstream HTTP calls)."""
    cid = get_correlation_id()
    if cid:
        return {**headers, "X-Correlation-ID": cid}
    return headers
