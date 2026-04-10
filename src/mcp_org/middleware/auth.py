"""Authentication middleware for enterprise MCP servers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable

import httpx


@dataclass
class AuthenticatedUser:
    email: str
    name: str
    teams: list[str]
    token: str


class OIDCAuthMiddleware:
    """Validate OIDC/OAuth2 bearer tokens against your IdP."""

    def __init__(
        self,
        issuer: str,
        client_id: str,
        jwks_cache_ttl: int = 3600,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._jwks_cache: dict = {}
        self._jwks_fetched_at: float = 0
        self._cache_ttl = jwks_cache_ttl

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        """Validate a bearer token and return the authenticated user."""
        try:
            import jwt
        except ImportError:
            raise RuntimeError("PyJWT required: pip install PyJWT cryptography")

        jwks = await self._get_jwks()

        try:
            payload = jwt.decode(
                token,
                jwks,
                algorithms=["RS256"],
                audience=self._client_id,
                issuer=self._issuer,
            )
        except jwt.InvalidTokenError as e:
            return None

        email = payload.get("email", "")
        name = payload.get("name", email)
        # Teams/groups claim varies by IdP: Okta uses "groups", Azure uses "roles"
        teams = (
            payload.get("groups", [])
            or payload.get("roles", [])
            or payload.get("teams", [])
        )

        return AuthenticatedUser(email=email, name=name, teams=teams, token=token)

    async def _get_jwks(self) -> Any:
        now = time.time()
        if now - self._jwks_fetched_at < self._cache_ttl and self._jwks_cache:
            return self._jwks_cache

        async with httpx.AsyncClient() as client:
            # Fetch OIDC discovery document
            discovery_resp = await client.get(
                f"{self._issuer}/.well-known/openid-configuration"
            )
            discovery_resp.raise_for_status()
            jwks_uri = discovery_resp.json()["jwks_uri"]

            # Fetch JWKS
            jwks_resp = await client.get(jwks_uri)
            jwks_resp.raise_for_status()
            self._jwks_cache = jwks_resp.json()
            self._jwks_fetched_at = now

        return self._jwks_cache


class APIKeyAuthMiddleware:
    """Simple API key authentication for non-SSO environments."""

    def __init__(self, valid_keys: dict[str, dict] | None = None) -> None:
        """
        valid_keys: {
            "key-value": {"email": "user@org.com", "teams": ["sre-team"], "name": "Sarat"}
        }
        If not provided, reads from ORG_MCP_API_KEYS env var as JSON.
        """
        if valid_keys:
            self._keys = valid_keys
        else:
            import json
            keys_json = os.environ.get("ORG_MCP_API_KEYS", "{}")
            self._keys = json.loads(keys_json)

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        user_info = self._keys.get(token)
        if not user_info:
            return None
        return AuthenticatedUser(
            email=user_info.get("email", "unknown"),
            name=user_info.get("name", "unknown"),
            teams=user_info.get("teams", []),
            token=token,
        )


class TeamBasedRBAC:
    """Control which teams can call which tools."""

    def __init__(self, rules: dict[str, list[str]]) -> None:
        """
        rules: {
            "sre-team": ["deploy_*", "get_*", "restart_*"],
            "dev-team": ["get_*", "search_*"],
            "*": ["get_oncall"],  # Everyone can call this
        }
        """
        self._rules = rules

    def can_call(self, user: AuthenticatedUser, tool_name: str) -> bool:
        """Return True if user is allowed to call the tool."""
        # Check wildcard rules first
        if self._matches_any(tool_name, self._rules.get("*", [])):
            return True

        # Check team-specific rules
        for team in user.teams:
            if self._matches_any(tool_name, self._rules.get(team, [])):
                return True

        return False

    def _matches_any(self, tool_name: str, patterns: list[str]) -> bool:
        import fnmatch
        return any(fnmatch.fnmatch(tool_name, pattern) for pattern in patterns)

    def get_allowed_tools(self, user: AuthenticatedUser, all_tools: list[str]) -> list[str]:
        """Return list of tools this user is allowed to call."""
        return [t for t in all_tools if self.can_call(user, t)]
