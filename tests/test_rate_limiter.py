"""Tests for the per-user per-tool token bucket rate limiter."""

from __future__ import annotations

import time

import pytest

from mcp_org.middleware.rate_limit import (
    RateLimitConfig,
    RateLimiter,
    TokenBucket,
    ToolQuota,
    TeamOverride,
)


# ─── TokenBucket Unit Tests ───────────────────────────────────────────────────

class TestTokenBucket:
    def test_full_bucket_allows_call(self):
        bucket = TokenBucket(hourly=100, burst=10)
        allowed, remaining, retry = bucket.consume()
        assert allowed is True
        assert remaining == 109  # 110 - 1

    def test_empty_bucket_denies(self):
        bucket = TokenBucket(hourly=1, burst=0)
        bucket._tokens = 0.0
        allowed, remaining, retry = bucket.consume()
        assert allowed is False
        assert remaining == 0
        assert retry > 0

    def test_burst_allows_rapid_calls(self):
        bucket = TokenBucket(hourly=1, burst=10)
        allowed_count = sum(1 for _ in range(11) if bucket.consume()[0])
        assert allowed_count == 11  # 1 hourly + 10 burst = 11 total

    def test_refill_over_time(self):
        bucket = TokenBucket(hourly=3600, burst=0)  # 1 token/second
        bucket._tokens = 0.0
        bucket._last_refill = time.monotonic() - 5  # Simulate 5 seconds elapsed
        allowed, remaining, _ = bucket.consume()
        assert allowed is True
        assert remaining >= 3  # at least 4 tokens refilled, 1 consumed

    def test_peek_remaining_nondestructive(self):
        bucket = TokenBucket(hourly=100, burst=10)
        initial = bucket.peek_remaining()
        assert initial == 110
        assert bucket.peek_remaining() == 110

    def test_capacity(self):
        assert TokenBucket(hourly=200, burst=20).capacity == 220

    def test_reset_at_in_future(self):
        bucket = TokenBucket(hourly=100, burst=10)
        assert bucket.reset_at_timestamp() >= time.time()

    def test_retry_after_reasonable(self):
        bucket = TokenBucket(hourly=3600, burst=0)  # 1 token/second
        bucket._tokens = 0.0
        _, _, retry = bucket.consume()
        assert 0.5 < retry < 2.0


# ─── RateLimitConfig Tests ────────────────────────────────────────────────────

class TestRateLimitConfig:
    def test_defaults_from_empty_env(self, monkeypatch):
        monkeypatch.delenv("RATE_LIMIT_CONFIG", raising=False)
        monkeypatch.delenv("RATE_LIMIT_DEFAULT", raising=False)
        monkeypatch.delenv("RATE_LIMIT_BURST", raising=False)
        config = RateLimitConfig.from_env()
        assert config.default_hourly == 200
        assert config.default_burst == 20

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.delenv("RATE_LIMIT_CONFIG", raising=False)
        monkeypatch.setenv("RATE_LIMIT_DEFAULT", "500")
        monkeypatch.setenv("RATE_LIMIT_BURST", "50")
        config = RateLimitConfig.from_env()
        assert config.default_hourly == 500
        assert config.default_burst == 50

    def test_json_config(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_CONFIG", """{
            "default_hourly": 300,
            "default_burst": 30,
            "team_overrides": {
                "sre-team": {"hourly_multiplier": 5.0, "burst_multiplier": 3.0}
            },
            "tool_overrides": {
                "trigger_chaos": {"hourly": 5, "burst": 1}
            }
        }""")
        config = RateLimitConfig.from_env()
        assert config.default_hourly == 300
        assert config.team_overrides["sre-team"].hourly_multiplier == 5.0
        assert config.tool_overrides["trigger_chaos"].hourly == 5

    def test_invalid_json_raises(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_CONFIG", "not-json")
        with pytest.raises(ValueError, match="not valid JSON"):
            RateLimitConfig.from_env()


# ─── RateLimiter Tests ────────────────────────────────────────────────────────

class TestRateLimiter:
    def _make_limiter(self, hourly=100, burst=10, **kwargs) -> RateLimiter:
        config = RateLimitConfig(default_hourly=hourly, default_burst=burst, **kwargs)
        return RateLimiter(config=config)

    def test_allows_first_call(self):
        result = self._make_limiter().check("alice@co", "get_oncall_schedule")
        assert result.allowed is True

    def test_deny_after_exhaustion(self):
        limiter = self._make_limiter(hourly=3, burst=0)
        results = [limiter.check("bob@co", "list_incidents").allowed for _ in range(4)]
        assert results[:3] == [True, True, True]
        assert results[3] is False

    def test_retry_after_populated_when_denied(self):
        limiter = self._make_limiter(hourly=1, burst=0)
        limiter.check("carol@co", "search_runbooks")
        result = limiter.check("carol@co", "search_runbooks")
        assert result.allowed is False
        assert result.retry_after_seconds > 0

    def test_rate_limit_result_headers_when_allowed(self):
        headers = self._make_limiter().check("dave@co", "get_alerts").as_headers()
        assert "X-RateLimit-Limit" in headers
        assert "X-RateLimit-Remaining" in headers
        assert "X-RateLimit-Reset" in headers
        assert "Retry-After" not in headers

    def test_retry_after_header_when_denied(self):
        limiter = self._make_limiter(hourly=1, burst=0)
        limiter.check("eve@co", "describe_incident")  # drain
        headers = limiter.check("eve@co", "describe_incident").as_headers()
        assert "Retry-After" in headers

    def test_tool_override_applies(self):
        config = RateLimitConfig(
            default_hourly=200,
            default_burst=20,
            tool_overrides={"expensive_tool": ToolQuota(hourly=3, burst=0)},
        )
        limiter = RateLimiter(config=config)
        results = [limiter.check("frank@co", "expensive_tool").allowed for _ in range(4)]
        assert results[:3] == [True, True, True]
        assert results[3] is False
        # Regular tool unaffected
        assert limiter.check("frank@co", "list_runbooks").allowed is True

    def test_team_override_increases_quota(self):
        config = RateLimitConfig(
            default_hourly=10,
            default_burst=0,
            team_overrides={"sre-team": TeamOverride(hourly_multiplier=10.0)},
        )
        limiter = RateLimiter(config=config)
        results = [limiter.check("grace@co", "list_alerts", ["sre-team"]).allowed for _ in range(11)]
        assert all(results)  # sre-team gets 100/hour, 11 calls should all pass

    def test_different_users_separate_buckets(self):
        limiter = self._make_limiter(hourly=2, burst=0)
        for _ in range(2):
            limiter.check("user1@co", "get_metrics")
        assert limiter.check("user1@co", "get_metrics").allowed is False
        assert limiter.check("user2@co", "get_metrics").allowed is True

    def test_different_tools_separate_buckets(self):
        limiter = self._make_limiter(hourly=1, burst=0)
        limiter.check("henry@co", "tool_a")  # drain tool_a
        assert limiter.check("henry@co", "tool_a").allowed is False
        assert limiter.check("henry@co", "tool_b").allowed is True

    def test_get_remaining_nondestructive(self):
        limiter = self._make_limiter(hourly=100, burst=10)
        r1 = limiter.get_remaining("iris@co", "search")
        r2 = limiter.get_remaining("iris@co", "search")
        assert r1 == r2

    def test_allow_convenience_method(self):
        limiter = self._make_limiter(hourly=1, burst=0)
        assert limiter.allow("jack@co", "tool") is True
        assert limiter.allow("jack@co", "tool") is False

    def test_reset_user_clears_all_buckets(self):
        limiter = self._make_limiter(hourly=1, burst=0)
        limiter.check("kate@co", "tool_x")
        assert limiter.check("kate@co", "tool_x").allowed is False
        limiter.reset_user("kate@co")
        assert limiter.check("kate@co", "tool_x").allowed is True

    def test_reset_user_tool_specific(self):
        limiter = self._make_limiter(hourly=1, burst=0)
        limiter.check("leo@co", "tool_a")
        limiter.check("leo@co", "tool_b")
        limiter.reset_user_tool("leo@co", "tool_a")
        assert limiter.check("leo@co", "tool_a").allowed is True
        assert limiter.check("leo@co", "tool_b").allowed is False  # still drained

    def test_stats_tracked_users(self):
        limiter = self._make_limiter()
        limiter.check("u1@co", "t")
        limiter.check("u2@co", "t")
        stats = limiter.stats()
        assert stats["tracked_users"] == 2

    def test_stats_throttled_count(self):
        limiter = self._make_limiter(hourly=1, burst=0)
        limiter.check("mike@co", "tool")
        limiter.check("mike@co", "tool")  # throttled
        assert limiter.stats()["users_currently_throttled"] >= 1

    def test_get_all_quotas(self):
        limiter = self._make_limiter(hourly=100, burst=10)
        limiter.check("nina@co", "tool_x")
        limiter.check("nina@co", "tool_y")
        quotas = limiter.get_all_quotas("nina@co")
        assert "tool:tool_x" in quotas
        assert quotas["tool:tool_x"]["capacity"] == 110

    def test_team_override_max_wins(self):
        """User in two teams gets the highest multiplier."""
        config = RateLimitConfig(
            default_hourly=10,
            default_burst=0,
            team_overrides={
                "team-a": TeamOverride(hourly_multiplier=2.0),
                "team-b": TeamOverride(hourly_multiplier=5.0),
            },
        )
        limiter = RateLimiter(config=config)
        # 5x * 10 = 50 allowed, 51st denied
        results = [limiter.check("oscar@co", "tool", ["team-a", "team-b"]).allowed for _ in range(51)]
        assert all(results[:50])
        assert results[50] is False

    def test_bucket_key_in_result(self):
        result = self._make_limiter().check("pat@co", "my_tool")
        assert "pat@co" in result.bucket_key
        assert "my_tool" in result.bucket_key
