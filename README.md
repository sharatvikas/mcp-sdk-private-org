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

## Federation Gateway: One Front Door for the Whole MCP Fleet

Once an org has more than one internal MCP server, pointing every client at every
server stops scaling: N clients × M servers means N×M credentials, N×M audit
trails, and no single place to enforce policy. The **federation gateway**
(`mcp_org/proxy.py`) collapses that to one connection per client:

```
   Claude Desktop        VS Code / Cursor        Slack bot
        │                       │                    │
        └───────────────────────┼────────────────────┘
                                │  one MCP endpoint (stdio / SSE)
                                ▼
        ┌───────────────────────────────────────────────┐
        │                 GatewayProxy                  │
        │                                               │
        │   auth (OIDC / API key)                       │
        │     → server-level team gate (allowed_teams)  │
        │       → tool-level RBAC (fnmatch rules)       │
        │         → token-bucket rate limit             │
        │           → audit log + correlation ID        │
        │                                               │
        │   ┌─────────────────────────────────────┐     │
        │   │ ServerRegistry (gateway.yaml,       │     │
        │   │ validated, hot-reloaded)            │     │
        │   └─────────────────────────────────────┘     │
        └───────┬───────────────┬───────────────┬───────┘
                │ stdio         │ SSE           │ streamable HTTP
                │ (subprocess)  │               │
                ▼               ▼               ▼
        ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
        │  runbooks    │ │  incidents   │ │   deploys    │
        │  MCP server  │ │  MCP server  │ │  MCP server  │
        └──────────────┘ └──────────────┘ └──────────────┘
         namespace:        namespace:        namespace:
         runbooks__*       incidents__*      deploy__*
```

**How a call flows.** The client sees one aggregate catalog with namespaced
tool names (`runbooks__search_runbooks`, `deploy__trigger_deploy`). A call to
`deploy__trigger_deploy` is resolved by prefix to the `deploys` server, passes
the four policy gates above, then is proxied over the downstream session with
the audit context (`correlation_id`, `principal`, `teams`) attached as MCP
`_meta` — so downstream logs join up with gateway logs end to end. Results
(including structured content and downstream `isError`) pass through untouched.

**Downstream health.** Each downstream connection is supervised: MCP pings at
`health_check_interval_seconds`, reconnect with exponential backoff (1s → 60s)
when a subprocess dies or a stream drops. Unhealthy servers vanish from the
catalog and calls to them return a structured `downstream_unavailable` error
instead of hanging. Per-server state is exported as the
`mcp_gateway_downstream_up` Prometheus gauge and via `downstream_health()`.

**Rate limiting.** Token buckets per principal + per namespaced tool
(sustained hourly rate + burst), with team multipliers and per-tool overrides.
A limited call returns a structured MCP error with `retry_after_seconds`,
increments `mcp_gateway_rate_limited_total`, and is written to the audit log
as `RATE_LIMITED`.

### Configuration

One YAML file drives the gateway (see [`config/gateway.example.yaml`](config/gateway.example.yaml)):

```yaml
gateway:
  name: org-mcp-gateway-proxy
  namespace_separator: "__"          # keeps tool names in [a-zA-Z0-9_-]
  hot_reload_interval_seconds: 30

rate_limits:
  default_hourly: 200
  default_burst: 20
  team_overrides:
    sre-team: { hourly_multiplier: 5.0, burst_multiplier: 3.0 }
  tool_overrides:
    deploy__trigger_deploy: { hourly: 10, burst: 2 }

rbac_rules:                          # tool-level, fnmatch on namespaced names
  sre-team: ["*"]
  dev-team: ["runbooks__*", "incidents__get_*"]
  "*": ["runbooks__search_runbooks"]

servers:                             # hot-reloadable: edit file, no restart
  - name: runbooks
    namespace: runbooks
    transport: stdio
    command: python
    args: ["-m", "runbooks_mcp_server"]
    allowed_teams: ["*"]
  - name: incidents
    namespace: incidents
    transport: sse
    url: http://incident-mcp.internal:8080/sse
    allowed_teams: [sre-team, dev-team]
  - name: deploys
    namespace: deploy
    transport: streamable-http
    url: https://deploy-mcp.internal/mcp
    allowed_teams: [sre-team]
```

Config is validated on load — every error is reported at once, and a bad hot
reload is rejected while the previous good config stays active. The registry
is also fully programmatic:

```python
from mcp_org.registry import ServerRegistry, DownstreamServerSpec
from mcp_org.proxy import GatewayProxy

registry = ServerRegistry.from_file("gateway.yaml")     # or ServerRegistry([specs...])
registry.register(DownstreamServerSpec(
    name="chaos", namespace="chaos", transport="stdio",
    command="/opt/chaos-mcp/bin/server", allowed_teams=("sre-team",),
))

proxy = GatewayProxy.from_config("gateway.yaml")
# python -m mcp_org.proxy gateway.yaml   ← CLI entrypoint (mcp-org-proxy)
```

### Gateway deployment notes

- **Kubernetes**: run `mcp-org-proxy` with the config in a ConfigMap mounted at
  `MCP_GATEWAY_CONFIG`. Hot reload picks up ConfigMap updates without a pod
  restart. Point liveness/readiness at the existing health server (`/health`,
  `/ready`); scrape `/metrics` for `mcp_gateway_downstream_up`,
  `mcp_gateway_rate_limited_total`, `mcp_gateway_rbac_denied_total`, and
  per-tool latency percentiles.
- **stdio downstreams** run as child processes of the gateway pod — package
  their runtimes into the gateway image (or use SSE/streamable-HTTP downstreams
  as sidecars/services and keep the gateway image slim).
- **Blast radius**: one crashed downstream only removes its namespace from the
  catalog; the supervisor reconnects with backoff while everything else keeps
  serving. Gateway boot does not block on unreachable downstreams.
- **Rate-limit state is in-memory per gateway instance.** Run one replica, or
  accept per-replica quotas, or swap the `RateLimiter` for a Redis-backed
  implementation before scaling horizontally.

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

## Try it locally

A self-contained, one-command demo proves the federation gateway (`GatewayProxy`) works end-to-end on localhost — no cloud, no bound network ports, everything over stdio subprocesses:

```bash
./demo/run_demo.sh
```

It boots the gateway with two real downstream MCP servers and walks the full request path: the RBAC-filtered aggregate catalog, routed tool calls proxied to each downstream, the token-bucket rate limiter rejecting with a structured `rate_limited` error, an RBAC denial, and the structured JSON audit log. See [`demo/OUTPUT.md`](demo/OUTPUT.md) for a captured real run and [`demo/README.md`](demo/README.md) for how it maps to the codebase.

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
│       ├── gateway.py          # Single-server MCP gateway (local tool handlers)
│       ├── proxy.py            # Federation gateway: multiplexes downstream MCP servers
│       ├── health.py           # /health /ready /metrics HTTP server + metrics registry
│       ├── middleware/
│       │   ├── auth.py         # OIDC + API key auth, team-based RBAC
│       │   ├── audit.py        # Audit logging to stdout/file/S3/CloudWatch
│       │   ├── correlation.py  # Correlation IDs bound to structlog context
│       │   └── rate_limit.py   # Token-bucket rate limiting (principal × tool)
│       ├── registry/
│       │   ├── loader.py       # Dynamic tool handler loading (plugins)
│       │   └── servers.py      # Downstream server registry (YAML/JSON, hot reload)
│       └── tools/              # Reference tool implementations
│           ├── incident_search.py
│           ├── postmortem.py
│           ├── runbooks.py
│           ├── audit_analytics.py
│           └── deployment_tracker.py
├── config/
│   └── gateway.example.yaml    # Federation gateway config example
└── tests/                      # 171 tests: routing, RBAC, rate limits, registry,
                                # health probes, end-to-end in-memory MCP round-trips
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
- [x] Token-bucket rate limiting (per principal × tool, team multipliers)
- [x] Correlation IDs + health/readiness probes + Prometheus metrics
- [x] Federation gateway: namespace routing to downstream MCP servers
- [x] Downstream server registry with validation + hot reload
- [x] Downstream health checks with reconnect/backoff
- [ ] Redis-backed rate limiter for horizontal gateway scaling
- [ ] SAML 2.0 support
- [ ] HashiCorp Vault integration for secrets
- [ ] Web UI for audit log exploration
- [ ] Terraform provider for tool configuration
- [ ] OpenTelemetry tracing

---

## License

MIT — see [LICENSE](LICENSE).
