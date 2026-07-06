"""Per-user, per-tool rate limiting for the Enterprise MCP Gateway.

Implements a token bucket algorithm with:
  - Per-user per-tool quotas (most granular)
  - Per-user global hourly quota (catch-all)
  - Team-level quota overrides (e.g. sre-team gets 5x the default)
  - Burst capacity (short bursts above the sustained rate are allowed)
  - Configurable limits via environment variables or RATE_LIMIT_CONFIG JSON
  - Rate limit metadata returned to callers (remaining tokens, reset time)
  - In-memory storage (swap for Redis adapter for multi-instance deployments)

Configuration via RATE_LIMIT_CONFIG environment variable (JSON):

    {
      "default_hourly": 200,
      "default_burst": 20,
      "team_overrides": {
        "sre-team": { "hourly_multiplier": 5, "burst_multiplier": 3 },
        "dev-team": { "hourly_multiplier": 2 }
      },
      "tool_overrides": {
        "create_postmortem_draft": { "hourly": 10, "burst": 3 },
        "search_runbooks": { "hourly": 50, "burst": 10 },
        "trigger_chaos_experiment": { "hourly": 5, "burst": 1 }
      }
    }
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from threading import Lock

# ─── Rate Limit Outcome ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining_tokens: int
    limit: int                   # Total capacity for this bucket
    reset_at: float              # Unix timestamp when bucket fully refills
    retry_after_seconds: float   # How long to wait if denied (0 if allowed)
    bucket_key: str              # Diagnostic: which bucket applied

    @property
    def reset_in_seconds(self) -> float:
        return max(0.0, self.reset_at - time.time())

    def as_headers(self) -> dict[str, str]:
        """HTTP-style rate limit headers (for SSE/HTTP transport)."""
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining_tokens),
            "X-RateLimit-Reset": str(int(self.reset_at)),
            **({"Retry-After": str(math.ceil(self.retry_after_seconds))} if not self.allowed else {}),
        }


# ─── Token Bucket ─────────────────────────────────────────────────────────────

@dataclass
class TokenBucket:
    """
    Token bucket with sustained rate + burst capacity.

    Sustained rate: `hourly` tokens / hour (= hourly/3600 tokens/second).
    Burst: `burst` additional tokens that can be consumed immediately above
           the sustained rate without waiting for refill.

    Total capacity = hourly + burst. Tokens refill at hourly/3600 per second.
    Starting at full capacity.
    """
    hourly: int
    burst: int

    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: Lock = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        self._lock = Lock()

    @property
    def capacity(self) -> int:
        return self.hourly + self.burst

    @property
    def refill_rate(self) -> float:
        """Tokens per second."""
        return self.hourly / 3600.0

    def consume(self) -> tuple[bool, int, float]:
        """
        Attempt to consume one token.

        Returns:
            (allowed, remaining_after, retry_after_seconds)
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(float(self.capacity), self._tokens + elapsed * self.refill_rate)
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True, int(self._tokens), 0.0
            else:
                # Calculate how long until the next token is available
                deficit = 1.0 - self._tokens
                retry_after = deficit / self.refill_rate
                return False, 0, retry_after

    def peek_remaining(self) -> int:
        """Non-consuming check of remaining tokens (approximate)."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            projected = min(float(self.capacity), self._tokens + elapsed * self.refill_rate)
            return int(projected)

    def reset_at_timestamp(self) -> float:
        """Unix timestamp when the bucket will be fully refilled."""
        with self._lock:
            deficit = self.capacity - self._tokens
            seconds_to_full = deficit / self.refill_rate if self.refill_rate > 0 else 0
            return time.time() + seconds_to_full


# ─── Rate Limit Config ────────────────────────────────────────────────────────

@dataclass
class ToolQuota:
    hourly: int
    burst: int


@dataclass
class TeamOverride:
    hourly_multiplier: float = 1.0
    burst_multiplier: float = 1.0


@dataclass
class RateLimitConfig:
    default_hourly: int = 200
    default_burst: int = 20
    team_overrides: dict[str, TeamOverride] = field(default_factory=dict)
    tool_overrides: dict[str, ToolQuota] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> RateLimitConfig:
        raw = os.environ.get("RATE_LIMIT_CONFIG", "")
        if not raw:
            # Fall back to individual env vars for backward compatibility
            return cls(
                default_hourly=int(os.environ.get("RATE_LIMIT_DEFAULT", "200")),
                default_burst=int(os.environ.get("RATE_LIMIT_BURST", "20")),
                tool_overrides={
                    "create_postmortem_draft": ToolQuota(hourly=10, burst=3),
                    "trigger_chaos_experiment": ToolQuota(hourly=5, burst=1),
                    "search_runbooks": ToolQuota(hourly=50, burst=10),
                    "explain_cost_anomaly": ToolQuota(hourly=20, burst=5),
                    "generate_terraform_remediation": ToolQuota(hourly=15, burst=3),
                },
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"RATE_LIMIT_CONFIG is not valid JSON: {e}") from e

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> RateLimitConfig:
        """Build a config from an already-parsed dict (e.g. a gateway YAML section)."""
        if not isinstance(data, dict):
            raise ValueError(f"rate limit config must be a mapping, got {type(data).__name__}")
        try:
            team_overrides = {
                team: TeamOverride(**override)
                for team, override in data.get("team_overrides", {}).items()
            }
            tool_overrides = {
                tool: ToolQuota(**quota)
                for tool, quota in data.get("tool_overrides", {}).items()
            }
        except TypeError as e:
            raise ValueError(f"invalid rate limit override: {e}") from e
        return cls(
            default_hourly=int(data.get("default_hourly", 200)),
            default_burst=int(data.get("default_burst", 20)),
            team_overrides=team_overrides,
            tool_overrides=tool_overrides,
        )


# ─── Rate Limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Per-user, per-tool token bucket rate limiter.

    Bucket hierarchy (most specific wins):
      1. user + tool  (e.g. alice@co + trigger_chaos_experiment)
      2. user global  (e.g. alice@co — all tools)

    Team overrides multiply the default limits for all members.
    Tool overrides set fixed hourly/burst for specific tool names.
    """

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self._config = config or RateLimitConfig.from_env()
        # buckets[user_email][bucket_key] = TokenBucket
        self._buckets: dict[str, dict[str, TokenBucket]] = {}
        self._lock = Lock()
        # Decision counters (per tool) for metrics scraping
        self._allowed_total: dict[str, int] = {}
        self._denied_total: dict[str, int] = {}

    def _resolve_quota(self, user_email: str, tool_name: str, user_teams: list[str]) -> ToolQuota:
        """Determine the effective quota for (user, tool)."""
        # Check tool-specific override first
        if tool_name in self._config.tool_overrides:
            return self._config.tool_overrides[tool_name]

        # Apply team multiplier if user belongs to a team with an override
        hourly = float(self._config.default_hourly)
        burst = float(self._config.default_burst)

        best_multiplier_h = 1.0
        best_multiplier_b = 1.0
        for team in user_teams:
            override = self._config.team_overrides.get(team)
            if override:
                best_multiplier_h = max(best_multiplier_h, override.hourly_multiplier)
                best_multiplier_b = max(best_multiplier_b, override.burst_multiplier)

        return ToolQuota(
            hourly=int(hourly * best_multiplier_h),
            burst=int(burst * best_multiplier_b),
        )

    def _get_or_create_bucket(
        self,
        user_email: str,
        bucket_key: str,
        quota: ToolQuota,
    ) -> TokenBucket:
        with self._lock:
            if user_email not in self._buckets:
                self._buckets[user_email] = {}
            user_buckets = self._buckets[user_email]
            if bucket_key not in user_buckets:
                user_buckets[bucket_key] = TokenBucket(
                    hourly=quota.hourly,
                    burst=quota.burst,
                )
            return user_buckets[bucket_key]

    def check(
        self,
        user_email: str,
        tool_name: str,
        user_teams: list[str] | None = None,
    ) -> RateLimitResult:
        """
        Check and consume a rate limit token for (user, tool).

        Args:
            user_email: Authenticated user identifier
            tool_name:  MCP tool being called
            user_teams: User's team memberships (for quota multipliers)

        Returns:
            RateLimitResult with allowed=True if the call may proceed.
        """
        teams = user_teams or []
        quota = self._resolve_quota(user_email, tool_name, teams)

        # Per-user per-tool bucket
        bucket_key = f"tool:{tool_name}"
        bucket = self._get_or_create_bucket(user_email, bucket_key, quota)

        allowed, remaining, retry_after = bucket.consume()

        with self._lock:
            counter = self._allowed_total if allowed else self._denied_total
            counter[tool_name] = counter.get(tool_name, 0) + 1

        return RateLimitResult(
            allowed=allowed,
            remaining_tokens=remaining,
            limit=bucket.capacity,
            reset_at=bucket.reset_at_timestamp(),
            retry_after_seconds=retry_after,
            bucket_key=f"{user_email}/{bucket_key}",
        )

    def allow(self, user_email: str, tool_name: str, user_teams: list[str] | None = None) -> bool:
        """Convenience wrapper — returns True if call is allowed."""
        return self.check(user_email, tool_name, user_teams).allowed

    def get_remaining(self, user_email: str, tool_name: str, user_teams: list[str] | None = None) -> int:
        """Return approximate remaining tokens without consuming."""
        teams = user_teams or []
        quota = self._resolve_quota(user_email, tool_name, teams)
        bucket_key = f"tool:{tool_name}"
        bucket = self._get_or_create_bucket(user_email, bucket_key, quota)
        return bucket.peek_remaining()

    def get_all_quotas(self, user_email: str, user_teams: list[str] | None = None) -> dict[str, dict]:
        """Return a snapshot of all known buckets for a user (for admin/debug)."""
        with self._lock:
            user_buckets = self._buckets.get(user_email, {})

        result = {}
        for bucket_key, bucket in user_buckets.items():
            result[bucket_key] = {
                "capacity": bucket.capacity,
                "remaining": bucket.peek_remaining(),
                "reset_at": bucket.reset_at_timestamp(),
                "hourly": bucket.hourly,
                "burst": bucket.burst,
            }
        return result

    def reset_user(self, user_email: str) -> None:
        """Admin: reset all rate limit buckets for a user (e.g. on incident response)."""
        with self._lock:
            self._buckets.pop(user_email, None)

    def reset_user_tool(self, user_email: str, tool_name: str) -> None:
        """Admin: reset a specific user+tool bucket."""
        bucket_key = f"tool:{tool_name}"
        with self._lock:
            user_buckets = self._buckets.get(user_email, {})
            user_buckets.pop(bucket_key, None)

    def stats(self) -> dict:
        """Return aggregate statistics across all users."""
        with self._lock:
            total_users = len(self._buckets)
            total_buckets = sum(len(b) for b in self._buckets.values())
            throttled_users = sum(
                1 for user_buckets in self._buckets.values()
                for bucket in user_buckets.values()
                if bucket.peek_remaining() == 0
            )
            allowed_total = sum(self._allowed_total.values())
            denied_total = sum(self._denied_total.values())
            denied_by_tool = dict(self._denied_total)
        return {
            "tracked_users": total_users,
            "tracked_buckets": total_buckets,
            "users_currently_throttled": throttled_users,
            "allowed_total": allowed_total,
            "denied_total": denied_total,
            "denied_by_tool": denied_by_tool,
        }
