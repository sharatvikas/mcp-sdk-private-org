# Local gateway demo

A self-contained, one-command demo that proves the enterprise MCP gateway
(`GatewayProxy`) works **end-to-end on localhost** — no cloud, no bound network
ports. Everything runs over stdio subprocesses.

## Run it

```bash
./demo/run_demo.sh
```

The script uses the repo's `.venv` (creating and `pip install -e .` into one if
absent), then runs `demo/run_demo.py`. The captured output of a real run is in
[`OUTPUT.md`](./OUTPUT.md).

## What it does

`run_demo.py` boots the gateway from [`gateway.demo.yaml`](./gateway.demo.yaml)
via `GatewayProxy.from_config(...)`. That config registers **two real downstream
MCP servers** as stdio subprocesses:

| Downstream | Namespace  | Tools                              | Allowed teams        |
|------------|------------|------------------------------------|----------------------|
| `runbooks` | `runbooks` | `search_runbooks`, `get_runbook`   | `sre-team`, `dev-team` |
| `metrics`  | `metrics`  | `list_metrics`, `query_metric`     | `sre-team`           |

The downstream servers live in [`downstreams/`](./downstreams/) and are ordinary
FastMCP servers — they know nothing about the gateway.

The demo then walks the full request path:

1. **Aggregate catalog** — the namespaced union of both downstream catalogs,
   RBAC-filtered per principal (an SRE sees 4 tools; a developer sees 2).
2. **Routed calls** — one tool call proxied to each downstream, showing the real
   downstream response returned through the gateway.
3. **Rate limiter** — hammers `runbooks__search_runbooks` (configured to
   `5/hour`, burst `0`) until the token bucket rejects with a structured
   `rate_limited` error including `retry_after_seconds`.
4. **RBAC denial** — a `dev-team` principal is denied on the `sre-team`-only
   `metrics` server (`forbidden`) before the call ever reaches the subprocess.
5. **Audit log** — prints the structured JSON audit entry the gateway wrote for
   every decision (allowed / rate-limited / denied).

## Files

- `run_demo.sh` — one-command runner (venv bootstrap + launch)
- `run_demo.py` — the driver that boots and exercises the gateway
- `gateway.demo.yaml` — the single config that drives the gateway (servers,
  `rate_limits`, `rbac_rules`)
- `downstreams/runbooks_server.py`, `downstreams/metrics_server.py` — minimal
  real downstream MCP servers
- `OUTPUT.md` — the exact command and real captured stdout

## How it maps to the codebase

- Config → `GatewayProxy.from_config` (`src/mcp_org/proxy.py`)
- Registry + hot reload → `ServerRegistry` (`src/mcp_org/registry/servers.py`)
- Routing / RBAC / rate-limit / audit pipeline → `GatewayProxy.dispatch`
- Rate limiter → `RateLimiter` (`src/mcp_org/middleware/rate_limit.py`)
- RBAC → `TeamBasedRBAC` (`src/mcp_org/middleware/auth.py`)
- Audit → `AuditLogger` (`src/mcp_org/middleware/audit.py`)
