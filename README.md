# mcp-sdk-private-org

> **A reference implementation and scaffold for building private, enterprise-grade MCP servers** within organizations. Includes authentication middleware, multi-tenant tool routing, audit logging, rate limiting, and deployment patterns for running MCP servers as internal company infrastructure.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![MCP SDK](https://img.shields.io/badge/MCP-1.x-green.svg)](https://modelcontextprotocol.io)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](Dockerfile)

---

## What This Is

The official MCP SDK is great for building tools quickly. But deploying a **private org MCP server** — one that multiple teams in a company share, with proper auth, tenancy, and compliance — requires a lot of additional plumbing that the SDK doesn't provide.

This project provides:
1. **A scaffold** for building private org MCP servers (copy, customize, deploy)
2. **Middleware library** for auth, audit, rate limiting — drop-in for any MCP server
3. **Deployment patterns** — K8s, Lambda, EC2 SSE server deployments
4. **Reference implementation** — a fully-featured "DevOps Toolbox" MCP server as a working example

---

## Architecture: Private Org MCP Server

```
┌──────────────────────────────────────────────────────────────────┐
│                   Enterprise Network                             │
│                                                                  │
│   ┌──────────────┐      ┌──────────────────────────────────┐    │
│   │ Claude Desktop│      │       MCP Gateway Server         │    │
│   │ (eng laptop) ├──────▶  ┌─────────────────────────────┐ │    │
│   └──────────────┘  SSE  │  │    Auth Middleware           │ │    │
│                          │  │  (OIDC/SAML/API key)        │ │    │
│   ┌──────────────┐       │  ├─────────────────────────────┤ │    │
│   │ VS Code/Cursor│──────▶  │    Tenant Router             │ │    │
│   └──────────────┘       │  │  (team → tool namespace)    │ │    │
│                          │  ├─────────────────────────────┤ │    │
│   ┌──────────────┐       │  │    Audit Logger              │ │    │
│   │  Slack Bot   │──────▶  │  (who called what, when)     │ │    │
│   └──────────────┘       │  ├─────────────────────────────┤ │    │
│                          │  │    Tool Registry             │ │    │
│                          │  │  (dynamically loaded)        │ │    │
│                          │  └──────────────┬──────────────┘ │    │
│                          └─────────────────┼────────────────┘    │
│                                            │                      │
│                          ┌─────────────────▼─────────────────┐   │
│                          │         Internal Tools             │   │
│                          │  Jira │ Confluence │ Jenkins │ AWS │   │
│                          │  Datadog │ Vault │ GitHub │ Slack  │   │
│                          └────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

---

## Key Problems Solved

### 1. Authentication & Authorization
```python
# Drop-in auth middleware
from mcp_org.middleware import OIDCAuthMiddleware, TeamBasedRBAC

server = MCPServer("my-org-mcp")

# OIDC/SSO auth — employees log in with company SSO
server.add_middleware(OIDCAuthMiddleware(
    issuer="https://sso.your-org.com",
    client_id="mcp-server",
))

# RBAC — control which teams can call which tools
server.add_middleware(TeamBasedRBAC({
    "sre-team": ["deploy_*", "get_*", "restart_*"],
    "dev-team": ["get_*", "search_*"],
    "security": ["*"],  # full access
}))
```

### 2. Audit Logging
Every tool invocation is logged with full context:
```json
{
  "timestamp": "2026-04-08T14:23:01Z",
  "user": "sarat@company.com",
  "team": "sre-team",
  "tool": "restart_service",
  "args": {"service": "payments-api", "namespace": "production"},
  "result_summary": "Restarted 3 pods successfully",
  "duration_ms": 4231,
  "session_id": "claude-desktop-abc123"
}
```

### 3. Multi-Tenant Tool Routing
Teams get isolated tool namespaces — sre-team gets infra tools, dev-team gets deployment tools:
```yaml
# config/tenants.yaml
tenants:
  sre-team:
    tools: [pagerduty, aws, kubernetes, datadog]
    rate_limit: 100/hour
  dev-team:
    tools: [jira, github, deployment-status]
    rate_limit: 200/hour
```

### 4. Dynamic Tool Registration
Tools are loaded from a registry — add new tools without restarting the server:
```python
# tools/deploy_service.py
@mcp_tool(
    name="deploy_service",
    description="Deploy a service to Kubernetes via ArgoCD",
    allowed_teams=["sre-team", "release-team"],
)
async def deploy_service(service: str, version: str, environment: str) -> str:
    ...
```

---

## Reference Implementation: DevOps Toolbox MCP

A fully-featured example server with these tools pre-built:

| Tool | Description | Auth Required |
|------|-------------|--------------|
| `search_runbooks` | Semantic search over Confluence runbooks | SSO |
| `get_deployment_status` | ArgoCD app health across environments | SSO |
| `get_recent_incidents` | PagerDuty incidents from last N hours | SSO |
| `get_service_metrics` | Datadog metrics for a service | SSO |
| `create_jira_ticket` | Create incident/task in Jira | SSO + RBAC |
| `restart_k8s_deployment` | Rolling restart of a deployment | SSO + RBAC (SRE only) |
| `get_vault_secret` | Read a secret from HashiCorp Vault | SSO + Vault policy |

---

## Deployment Options

### Option 1: Kubernetes (recommended for orgs)
```bash
helm install mcp-org-server ./charts/mcp-org-server \
  --set auth.oidcIssuer=https://sso.your-org.com \
  --set audit.destination=s3://mcp-audit-logs \
  --namespace mcp-platform --create-namespace
```

### Option 2: AWS Lambda + API Gateway (low-traffic)
```bash
cd deployments/lambda && terraform apply
# Provisions: Lambda + API GW + WAF + CloudWatch logs
```

### Option 3: Standalone SSE Server (small teams)
```bash
docker compose up -d
# Exposes SSE at https://mcp.your-org.internal:8443
```

---

## Project Structure

```
mcp-sdk-private-org/
├── src/
│   └── mcp_org/
│       ├── gateway.py          # Main MCP gateway server
│       ├── middleware/
│       │   ├── auth.py         # OIDC, API key auth
│       │   ├── rbac.py         # Team-based access control
│       │   ├── audit.py        # Audit logging to S3/CloudWatch
│       │   └── rate_limit.py   # Per-tenant rate limiting
│       ├── registry/
│       │   ├── loader.py       # Dynamic tool loading
│       │   └── schema.py       # Tool registration schema
│       └── tools/              # Example tool implementations
│           ├── pagerduty.py
│           ├── jira.py
│           ├── github.py
│           └── kubernetes.py
├── deployments/
│   ├── kubernetes/             # Helm chart
│   ├── lambda/                 # Terraform for Lambda deployment
│   └── docker-compose.yml
├── docs/
│   ├── ARCHITECTURE.md
│   ├── ADDING_TOOLS.md         # Guide for adding new tools
│   ├── AUTH_SETUP.md           # OIDC/SAML configuration guide
│   └── COMPLIANCE.md           # Audit log schema + retention
└── examples/
    └── devops-toolbox/         # Full reference implementation
```

---

## Why Not Just Use the Official MCP SDK Directly?

The official SDK handles the protocol. This project handles everything else that enterprises actually need:

| Concern | Official SDK | This Project |
|---------|-------------|--------------|
| Protocol implementation | ✅ | ✅ (wraps SDK) |
| Authentication | ❌ | ✅ OIDC/SAML/API key |
| Authorization (RBAC) | ❌ | ✅ Team-based |
| Audit logging | ❌ | ✅ Structured JSON |
| Rate limiting | ❌ | ✅ Per-tenant |
| Multi-tenant routing | ❌ | ✅ |
| Production deployment | ❌ | ✅ K8s/Lambda/Docker |
| Tool hot-reload | ❌ | ✅ |

---

## Roadmap

- [x] OIDC authentication middleware
- [x] Team-based RBAC
- [x] Audit logging to S3
- [x] Dynamic tool registry
- [ ] SAML 2.0 support
- [ ] HashiCorp Vault integration for secrets
- [ ] Web UI for audit log exploration
- [ ] Terraform provider for tool configuration
- [ ] OpenTelemetry tracing

---

## License

MIT — see [LICENSE](LICENSE).
