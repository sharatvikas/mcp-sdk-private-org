"""Per-tenant rate limiting for the org MCP gateway."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class RateLimitBucket:
    """Token bucket for rate limiting."""
    capacity: int
    tokens: float
    last_refill: float = field(default_factory=time.monotonic)
    refill_rate: float = 0.0  # tokens per second

    def __post_init__(self) -> None:
        self.refill_rate = self.capacity / 3600.0  # refill over 1 hour

    def consume(self) -> bool:
        """Consume one token. Returns True if allowed, False if rate limited."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class RateLimiter:
    """
    Per-user, per-tool rate limiting with configurable limits.

    Default limits (per hour):
      - Default: 200 calls/hour
      - Expensive tools (AI analysis): 20 calls/hour
    """

    EXPENSIVE_TOOLS = {
        "create_postmortem_draft",
        "search_runbooks",  # Uses embeddings
    }

    def __init__(self) -> None:
        self._buckets: dict[str, dict[str, RateLimitBucket]] = defaultdict(dict)
        self._lock = Lock()
        self._default_limit = int(os.environ.get("RATE_LIMIT_DEFAULT", "200"))
        self._expensive_limit = int(os.environ.get("RATE_LIMIT_EXPENSIVE", "20"))

    def allow(self, user_email: str, tool_name: str) -> bool:
        """Return True if the call should be allowed."""
        limit = self._expensive_limit if tool_name in self.EXPENSIVE_TOOLS else self._default_limit
        bucket_key = f"{tool_name}:{'expensive' if tool_name in self.EXPENSIVE_TOOLS else 'default'}"

        with self._lock:
            if bucket_key not in self._buckets[user_email]:
                self._buckets[user_email][bucket_key] = RateLimitBucket(
                    capacity=limit,
                    tokens=float(limit),
                )
            return self._buckets[user_email][bucket_key].consume()

    def get_remaining(self, user_email: str, tool_name: str) -> int:
        """Return approximate remaining tokens for a user/tool."""
        bucket_key = f"{tool_name}:{'expensive' if tool_name in self.EXPENSIVE_TOOLS else 'default'}"
        with self._lock:
            bucket = self._buckets.get(user_email, {}).get(bucket_key)
            if not bucket:
                return self._default_limit
            return int(bucket.tokens)
