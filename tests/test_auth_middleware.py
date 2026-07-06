"""Tests for authentication and RBAC middleware.

Tests cover:
- APIKeyAuthMiddleware: valid key, invalid key, missing key
- OIDCAuthMiddleware: valid JWT, expired token, wrong audience, malformed token
- TeamBasedRBAC: wildcard rules, team rules, multi-team, unknown team
- Integration: full auth → RBAC pipeline
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_org.middleware.auth import (
    APIKeyAuthMiddleware,
    AuthenticatedUser,
    OIDCAuthMiddleware,
    TeamBasedRBAC,
)

# ── APIKeyAuthMiddleware ───────────────────────────────────────────────────────


class TestAPIKeyAuthMiddleware:
    @pytest.fixture
    def valid_keys(self) -> dict:
        return {
            "sre-key-abc123": {
                "email": "sarat@sovrn.com",
                "name": "Sarat",
                "teams": ["sre-team", "platform-team"],
            },
            "dev-key-xyz789": {
                "email": "dev@sovrn.com",
                "name": "Developer",
                "teams": ["dev-team"],
            },
        }

    @pytest.fixture
    def middleware(self, valid_keys) -> APIKeyAuthMiddleware:
        return APIKeyAuthMiddleware(valid_keys=valid_keys)

    @pytest.mark.asyncio
    async def test_valid_key_returns_user(self, middleware):
        user = await middleware.authenticate("sre-key-abc123")
        assert user is not None
        assert user.email == "sarat@sovrn.com"
        assert user.name == "Sarat"
        assert "sre-team" in user.teams
        assert "platform-team" in user.teams

    @pytest.mark.asyncio
    async def test_invalid_key_returns_none(self, middleware):
        user = await middleware.authenticate("invalid-key-00000")
        assert user is None

    @pytest.mark.asyncio
    async def test_empty_key_returns_none(self, middleware):
        user = await middleware.authenticate("")
        assert user is None

    @pytest.mark.asyncio
    async def test_dev_team_key_has_correct_teams(self, middleware):
        user = await middleware.authenticate("dev-key-xyz789")
        assert user is not None
        assert user.teams == ["dev-team"]
        assert "sre-team" not in user.teams

    @pytest.mark.asyncio
    async def test_loads_from_env_var(self, monkeypatch):
        keys = {
            "env-key-test": {
                "email": "test@test.com",
                "name": "Test User",
                "teams": ["test-team"],
            }
        }
        monkeypatch.setenv("ORG_MCP_API_KEYS", json.dumps(keys))
        middleware = APIKeyAuthMiddleware()
        user = await middleware.authenticate("env-key-test")
        assert user is not None
        assert user.email == "test@test.com"

    @pytest.mark.asyncio
    async def test_empty_env_var_returns_none(self, monkeypatch):
        monkeypatch.setenv("ORG_MCP_API_KEYS", "{}")
        middleware = APIKeyAuthMiddleware()
        user = await middleware.authenticate("any-key")
        assert user is None


# ── OIDCAuthMiddleware ────────────────────────────────────────────────────────


class TestOIDCAuthMiddleware:
    """Tests for OIDCAuthMiddleware.

    We mock PyJWT and the JWKS fetch so these tests run without a real IdP.
    """

    ISSUER = "https://auth.sovrn.com"
    CLIENT_ID = "mcp-gateway"

    @pytest.fixture
    def middleware(self) -> OIDCAuthMiddleware:
        return OIDCAuthMiddleware(
            issuer=self.ISSUER,
            client_id=self.CLIENT_ID,
            jwks_cache_ttl=60,
        )

    @pytest.fixture
    def valid_payload(self) -> dict:
        now = int(time.time())
        return {
            "iss": self.ISSUER,
            "aud": self.CLIENT_ID,
            "sub": "user|abc123",
            "email": "sarat@sovrn.com",
            "name": "Sarat Jogavajjala",
            "groups": ["sre-team", "platform-team"],
            "iat": now - 60,
            "exp": now + 3600,
        }

    @pytest.mark.asyncio
    async def test_valid_token_returns_user(self, middleware, valid_payload):
        fake_jwks = {"keys": [{"kid": "test", "kty": "RSA"}]}

        with patch.object(middleware, "_get_jwks", return_value=fake_jwks):
            with patch("jwt.decode", return_value=valid_payload):
                user = await middleware.authenticate("valid.jwt.token")

        assert user is not None
        assert user.email == "sarat@sovrn.com"
        assert user.name == "Sarat Jogavajjala"
        assert "sre-team" in user.teams

    @pytest.mark.asyncio
    async def test_expired_token_returns_none(self, middleware):
        fake_jwks = {"keys": [{"kid": "test"}]}

        import jwt as pyjwt

        with patch.object(middleware, "_get_jwks", return_value=fake_jwks):
            with patch("jwt.decode", side_effect=pyjwt.ExpiredSignatureError("expired")):
                user = await middleware.authenticate("expired.jwt.token")

        assert user is None

    @pytest.mark.asyncio
    async def test_wrong_audience_returns_none(self, middleware):
        fake_jwks = {"keys": []}

        import jwt as pyjwt

        with patch.object(middleware, "_get_jwks", return_value=fake_jwks):
            with patch("jwt.decode", side_effect=pyjwt.InvalidAudienceError("bad audience")):
                user = await middleware.authenticate("wrong-aud.token")

        assert user is None

    @pytest.mark.asyncio
    async def test_malformed_token_returns_none(self, middleware):
        fake_jwks = {"keys": []}

        import jwt as pyjwt

        with patch.object(middleware, "_get_jwks", return_value=fake_jwks):
            with patch("jwt.decode", side_effect=pyjwt.DecodeError("malformed")):
                user = await middleware.authenticate("not.a.jwt")

        assert user is None

    @pytest.mark.asyncio
    async def test_groups_from_okta_claim(self, middleware):
        """Okta uses 'groups' claim for team membership."""
        payload = {
            "email": "user@sovrn.com",
            "name": "User",
            "groups": ["sre-team"],
            "roles": [],
        }
        with patch.object(middleware, "_get_jwks", return_value={}):
            with patch("jwt.decode", return_value=payload):
                user = await middleware.authenticate("okta.token.here")
        assert user is not None
        assert user.teams == ["sre-team"]

    @pytest.mark.asyncio
    async def test_roles_from_azure_claim(self, middleware):
        """Azure AD uses 'roles' claim for team membership."""
        payload = {
            "email": "user@sovrn.com",
            "name": "User",
            "groups": [],
            "roles": ["platform-team", "admin"],
        }
        with patch.object(middleware, "_get_jwks", return_value={}):
            with patch("jwt.decode", return_value=payload):
                user = await middleware.authenticate("azure.token.here")
        assert user is not None
        assert "platform-team" in user.teams

    @pytest.mark.asyncio
    async def test_jwks_caching(self, middleware):
        """JWKS should be fetched only once within the cache TTL."""
        fake_jwks = {"keys": [{"kid": "cached"}]}
        payload = {"email": "u@e.com", "name": "U", "groups": []}

        fetch_count = 0

        async def mock_get_jwks_inner():
            nonlocal fetch_count
            fetch_count += 1
            return fake_jwks

        # Populate cache first
        middleware._jwks_cache = fake_jwks
        middleware._jwks_fetched_at = time.time()

        with patch("jwt.decode", return_value=payload):
            await middleware.authenticate("token1")
            await middleware.authenticate("token2")
            await middleware.authenticate("token3")

        # Cache was warm — no HTTP fetches should have happened
        assert fetch_count == 0

    @pytest.mark.asyncio
    async def test_jwks_cache_expires_and_refetches(self, middleware):
        """After TTL expires, JWKS should be re-fetched."""
        payload = {"email": "u@e.com", "name": "U", "groups": []}
        fake_jwks = {"keys": []}

        # Expire the cache
        middleware._jwks_fetched_at = 0

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=None)
        mock_http.get = AsyncMock(return_value=MagicMock(
            json=lambda: {"jwks_uri": "https://auth.sovrn.com/.well-known/jwks.json"},
            raise_for_status=lambda: None,
        ))

        discovery_response = MagicMock(
            json=lambda: {"jwks_uri": "https://auth.sovrn.com/.well-known/jwks.json"},
            raise_for_status=lambda: None,
        )
        jwks_response = MagicMock(
            json=lambda: fake_jwks,
            raise_for_status=lambda: None,
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[discovery_response, jwks_response])
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch("jwt.decode", return_value=payload):
                await middleware.authenticate("fresh.token")

            # JWKS was fetched (discovery + jwks = 2 calls)
            assert mock_client.get.call_count == 2


# ── TeamBasedRBAC ─────────────────────────────────────────────────────────────


class TestTeamBasedRBAC:
    @pytest.fixture
    def rbac(self) -> TeamBasedRBAC:
        return TeamBasedRBAC(
            rules={
                "*": ["get_oncall", "search_runbooks"],
                "sre-team": ["deploy_*", "get_*", "restart_*", "correlate_*"],
                "platform-team": ["get_*", "list_*", "create_*"],
                "dev-team": ["get_*", "search_*"],
            }
        )

    @pytest.fixture
    def sre_user(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            email="sre@sovrn.com",
            name="SRE Engineer",
            teams=["sre-team"],
            token="sre-token",
        )

    @pytest.fixture
    def dev_user(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            email="dev@sovrn.com",
            name="Developer",
            teams=["dev-team"],
            token="dev-token",
        )

    @pytest.fixture
    def multi_team_user(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            email="lead@sovrn.com",
            name="Tech Lead",
            teams=["sre-team", "platform-team"],
            token="lead-token",
        )

    def test_wildcard_rule_allows_everyone(self, rbac, sre_user, dev_user):
        assert rbac.can_call(sre_user, "get_oncall") is True
        assert rbac.can_call(dev_user, "get_oncall") is True
        assert rbac.can_call(sre_user, "search_runbooks") is True

    def test_sre_team_can_deploy(self, rbac, sre_user):
        assert rbac.can_call(sre_user, "deploy_service") is True
        assert rbac.can_call(sre_user, "deploy_canary") is True

    def test_dev_team_cannot_deploy(self, rbac, dev_user):
        assert rbac.can_call(dev_user, "deploy_service") is False

    def test_sre_team_can_correlate(self, rbac, sre_user):
        assert rbac.can_call(sre_user, "correlate_alerts") is True

    def test_dev_team_cannot_correlate(self, rbac, dev_user):
        assert rbac.can_call(dev_user, "correlate_alerts") is False

    def test_multi_team_user_has_union_of_permissions(self, rbac, multi_team_user):
        # From sre-team
        assert rbac.can_call(multi_team_user, "deploy_service") is True
        assert rbac.can_call(multi_team_user, "correlate_alerts") is True
        # From platform-team
        assert rbac.can_call(multi_team_user, "create_namespace") is True
        assert rbac.can_call(multi_team_user, "list_nodes") is True

    def test_unknown_team_only_has_wildcard_access(self, rbac):
        unknown_user = AuthenticatedUser(
            email="ext@sovrn.com",
            name="External",
            teams=["external-team"],
            token="ext-token",
        )
        assert rbac.can_call(unknown_user, "get_oncall") is True  # wildcard
        assert rbac.can_call(unknown_user, "deploy_service") is False

    def test_get_allowed_tools_filters_correctly(self, rbac, dev_user):
        all_tools = [
            "get_oncall", "search_runbooks", "get_metrics",
            "deploy_service", "correlate_alerts", "list_incidents",
        ]
        allowed = rbac.get_allowed_tools(dev_user, all_tools)
        assert "get_oncall" in allowed        # wildcard
        assert "search_runbooks" in allowed   # wildcard
        assert "get_metrics" in allowed       # get_* matches dev-team
        assert "deploy_service" not in allowed
        assert "correlate_alerts" not in allowed
        assert "list_incidents" not in allowed  # list_* not in dev-team rules

    def test_exact_tool_name_match(self, rbac, sre_user):
        """sre-team has restart_* — should match restart_pod exactly."""
        assert rbac.can_call(sre_user, "restart_pod") is True
        assert rbac.can_call(sre_user, "restart_deployment") is True


# ── Integration: Auth + RBAC pipeline ────────────────────────────────────────


class TestAuthRBACIntegration:
    """Tests that simulate the full auth → RBAC pipeline."""

    @pytest.mark.asyncio
    async def test_authenticated_sre_can_call_deploy_tool(self):
        api_auth = APIKeyAuthMiddleware(valid_keys={
            "sre-key": {"email": "sre@sovrn.com", "name": "SRE", "teams": ["sre-team"]},
        })
        rbac = TeamBasedRBAC(rules={
            "*": ["search_runbooks"],
            "sre-team": ["deploy_*", "correlate_*"],
        })

        user = await api_auth.authenticate("sre-key")
        assert user is not None
        assert rbac.can_call(user, "deploy_canary") is True
        assert rbac.can_call(user, "correlate_alerts") is True

    @pytest.mark.asyncio
    async def test_unauthenticated_request_denied(self):
        api_auth = APIKeyAuthMiddleware(valid_keys={
            "sre-key": {"email": "sre@sovrn.com", "name": "SRE", "teams": ["sre-team"]},
        })

        user = await api_auth.authenticate("wrong-key")
        assert user is None
        # No RBAC check — unauthenticated requests are rejected at auth layer

    @pytest.mark.asyncio
    async def test_authenticated_dev_blocked_from_deploy(self):
        api_auth = APIKeyAuthMiddleware(valid_keys={
            "dev-key": {"email": "dev@sovrn.com", "name": "Dev", "teams": ["dev-team"]},
        })
        rbac = TeamBasedRBAC(rules={
            "sre-team": ["deploy_*"],
            "dev-team": ["get_*"],
        })

        user = await api_auth.authenticate("dev-key")
        assert user is not None
        assert rbac.can_call(user, "deploy_canary") is False
        assert rbac.can_call(user, "get_metrics") is True
