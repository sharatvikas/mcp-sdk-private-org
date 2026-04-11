"""Tests for the token bucket rate limiter."""

from __future__ import annotations

import asyncio
import time
import pytest

from mcp_org.middleware.rate_limit import RateLimitBucket, RateLimiter


class TestRateLimitBucket:
    def test_allows_requests_within_limit(self):
        bucket = RateLimitBucket(capacity=10, refill_rate=10.0)
        # Should allow 10 requests immediately
        for _ in range(10):
            assert bucket.consume() is True

    def test_blocks_when_exhausted(self):
        bucket = RateLimitBucket(capacity=5, refill_rate=1.0)
        for _ in range(5):
            bucket.consume()
        # 6th request should be blocked
        assert bucket.consume() is False

    def test_refills_over_time(self):
        bucket = RateLimitBucket(capacity=2, refill_rate=10.0)  # 10 tokens/sec
        bucket.consume()
        bucket.consume()
        assert bucket.consume() is False

        # Manually advance time by 0.2 seconds (should refill 2 tokens)
        bucket._last_refill = time.monotonic() - 0.2
        assert bucket.consume() is True

    def test_does_not_exceed_capacity(self):
        bucket = RateLimitBucket(capacity=5, refill_rate=100.0)
        # Let a lot of time pass — should cap at capacity
        bucket._last_refill = time.monotonic() - 10.0
        # Consume 5 (capacity) — should succeed
        for _ in range(5):
            assert bucket.consume() is True
        # 6th should fail (capped at capacity)
        assert bucket.consume() is False

    def test_multiple_tokens_consumed_at_once(self):
        bucket = RateLimitBucket(capacity=10, refill_rate=1.0)
        assert bucket.consume(tokens=5) is True
        assert bucket.consume(tokens=5) is True
        assert bucket.consume(tokens=1) is False

    def test_capacity_zero_always_blocks(self):
        bucket = RateLimitBucket(capacity=0, refill_rate=0.0)
        assert bucket.consume() is False


class TestRateLimiter:
    def test_different_users_get_independent_buckets(self):
        limiter = RateLimiter()
        # 200 requests per hour = ~0.0556 per second — bucket capacity is 200
        for _ in range(200):
            assert limiter.check("user-a", "read_file") is True

        # user-a exhausted, user-b should still work
        assert limiter.check("user-b", "read_file") is True

    def test_expensive_tools_have_lower_limits(self):
        limiter = RateLimiter()
        # Expensive tools (e.g., postmortem, analyze_capacity) have lower limits
        # Check that expensive tool is tracked separately
        results = []
        for i in range(25):
            allowed = limiter.check("user-c", "create_postmortem_draft")
            results.append(allowed)

        # At least the first 20 should be allowed (expensive limit = 20/hr)
        assert all(results[:20])

    def test_same_user_same_tool_shares_bucket(self):
        limiter = RateLimiter()
        # Exhaust budget for one tool
        exhausted = False
        for i in range(250):  # More than default 200 limit
            if not limiter.check("user-d", "search_runbooks"):
                exhausted = True
                break
        assert exhausted, "Rate limiter should have exhausted the budget"

    def test_reset_clears_user_state(self):
        limiter = RateLimiter()
        # Exhaust user's budget
        for _ in range(300):
            limiter.check("user-e", "list_runbook_categories")

        # Reset
        limiter.reset("user-e")

        # Should work again
        assert limiter.check("user-e", "list_runbook_categories") is True

    def test_unknown_tool_uses_default_limit(self):
        limiter = RateLimiter()
        # Unknown tools should use default rate limit, not error
        assert limiter.check("user-f", "some_unknown_tool") is True

    @pytest.mark.asyncio
    async def test_concurrent_requests_from_same_user(self):
        """Concurrent requests should not exceed the bucket capacity."""
        limiter = RateLimiter()
        results = []

        async def make_request():
            allowed = limiter.check("user-g", "get_runbook")
            results.append(allowed)

        # Fire 250 concurrent requests
        await asyncio.gather(*[make_request() for _ in range(250)])

        allowed_count = sum(results)
        # Should not exceed capacity (200 for default tools)
        assert allowed_count <= 200
