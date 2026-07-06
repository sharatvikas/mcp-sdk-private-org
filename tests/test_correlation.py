"""Tests for the request correlation ID middleware."""

import asyncio

import structlog

from mcp_org.middleware.correlation import (
    bind_correlation_id,
    clear_correlation_id,
    get_correlation_id,
    inject_into_headers,
)


class TestBindCorrelationId:
    def test_generates_uuid_when_none_given(self):
        cid = bind_correlation_id()
        assert len(cid) == 36  # UUID4 canonical form
        assert cid.count("-") == 4

    def test_accepts_existing_id(self):
        cid = bind_correlation_id("my-trace-abc123")
        assert cid == "my-trace-abc123"

    def test_get_returns_bound_id(self):
        bind_correlation_id("test-id-1")
        assert get_correlation_id() == "test-id-1"

    def test_each_call_generates_unique_id(self):
        ids = {bind_correlation_id() for _ in range(50)}
        assert len(ids) == 50

    def test_clear_resets_to_empty(self):
        bind_correlation_id("will-be-cleared")
        clear_correlation_id()
        assert get_correlation_id() == ""

    def test_structlog_contextvars_bound(self):
        """After bind_correlation_id, structlog contextvars should carry the ID."""
        structlog.contextvars.clear_contextvars()
        cid = bind_correlation_id()
        ctx = structlog.contextvars.get_contextvars()
        assert ctx.get("correlation_id") == cid

    def test_structlog_contextvars_cleared(self):
        bind_correlation_id("ephemeral")
        clear_correlation_id()
        ctx = structlog.contextvars.get_contextvars()
        assert "correlation_id" not in ctx


class TestContextvarIsolation:
    """Correlation IDs must not leak between concurrent asyncio tasks."""

    def test_tasks_have_independent_correlation_ids(self):
        async def task(name: str) -> tuple[str, str]:
            cid = bind_correlation_id(f"task-{name}")
            await asyncio.sleep(0)  # yield to event loop
            return cid, get_correlation_id()

        async def run():
            results = await asyncio.gather(
                task("alpha"),
                task("beta"),
                task("gamma"),
            )
            return results

        results = asyncio.run(run())
        # Each task should see its own ID — no cross-task leakage
        for name, (bound, retrieved) in zip(["alpha", "beta", "gamma"], results):
            assert bound == f"task-{name}"
            assert retrieved == f"task-{name}"


class TestInjectIntoHeaders:
    def test_adds_header_when_id_set(self):
        bind_correlation_id("hdr-test-id")
        headers = inject_into_headers({"Content-Type": "application/json"})
        assert headers["X-Correlation-ID"] == "hdr-test-id"
        assert headers["Content-Type"] == "application/json"  # existing preserved

    def test_no_header_when_id_empty(self):
        clear_correlation_id()
        headers = inject_into_headers({"Content-Type": "text/plain"})
        assert "X-Correlation-ID" not in headers

    def test_does_not_mutate_original_headers(self):
        bind_correlation_id("immutable-test")
        original = {"Accept": "application/json"}
        result = inject_into_headers(original)
        assert "X-Correlation-ID" not in original
        assert "X-Correlation-ID" in result
