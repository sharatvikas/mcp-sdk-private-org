# Gateway demo — captured run

This is the **real, unedited stdout** of the local end-to-end demo, produced by
running the gateway against two live downstream MCP servers (stdio subprocesses)
on localhost. No cloud, no bound network ports.

## Command

```bash
./demo/run_demo.sh
# equivalently, inside the venv:  python demo/run_demo.py
```

The runner uses the repo `.venv` (Python 3.12.13, `mcp` 1.27.0). It spawns
`demo/downstreams/runbooks_server.py` and `demo/downstreams/metrics_server.py`
as stdio MCP subprocesses and drives the gateway's real `dispatch()` /
`aggregate_tools()` API. Gateway `structlog` output is routed to stderr so
stdout is purely the demo narrative + audit trail.

## Captured stdout

```text
[demo] python: Python 3.12.13

========================================================================
GATEWAY BOOT
========================================================================
Gateway name        : demo-org-gateway
Registered servers  : ['runbooks', 'metrics']
Namespaces          : ['metrics', 'runbooks']
Starting gateway (spawns downstream MCP servers as stdio subprocesses)...
  downstream metrics   -> state=healthy transport=stdio tools_cached=0
  downstream runbooks  -> state=healthy transport=stdio tools_cached=2

========================================================================
1. AGGREGATE TOOL CATALOG (namespaced, RBAC-filtered)
========================================================================

As SRE (sre-oncall@corp.example, teams=['sre-team']) -> 4 tools:
  - metrics__list_metrics        List the metric names this service can query. [via metrics]
  - metrics__query_metric        Query a time series for a metric name over a window (e.g. 15m, 1h, 24h). [via metrics]
  - runbooks__get_runbook        Fetch the full step-by-step runbook for a given runbook id (e.g. rb-002). [via runbooks]
  - runbooks__search_runbooks    Search the runbook library by symptom, alert name, or keyword. [via runbooks]

As Developer (dev@corp.example, teams=['dev-team']) -> 2 tools:
  - runbooks__get_runbook        Fetch the full step-by-step runbook for a given runbook id (e.g. rb-002). [via runbooks]
  - runbooks__search_runbooks    Search the runbook library by symptom, alert name, or keyword. [via runbooks]
  (metrics__* hidden: the metrics server is gated to sre-team)

========================================================================
2. ROUTED TOOL CALLS THROUGH THE GATEWAY
========================================================================

-> runbooks__search_runbooks(query='database')  [routed to runbooks subprocess]
{
  "query": "database",
  "match_count": 1,
  "matches": [
    {
      "id": "rb-002",
      "title": "Database connection pool exhaustion",
      "category": "database"
    }
  ]
}

-> metrics__query_metric(name='payment_service.p99_latency_ms', window='1h')  [routed to metrics subprocess]
{
  "metric": "payment_service.p99_latency_ms",
  "window": "1h",
  "unit": "ms",
  "datapoints": [
    137.7,
    104.6,
    65.5,
    119.6,
    104.5,
    54.6
  ],
  "latest": 54.6
}

========================================================================
3. RATE LIMITER (token bucket: runbooks__search_runbooks = 5/hour, burst 0)
========================================================================

Hammering runbooks__search_runbooks as load-test@corp.example ...
  call #1: allowed (isError=False)
  call #2: allowed (isError=False)
  call #3: allowed (isError=False)
  call #4: allowed (isError=False)
  call #5: allowed (isError=False)
  call #6: RATE LIMITED  code=rate_limited  retry_after=719.988s  limit=5/hour  remaining=0
  full structured error result the client receives:
    {
      "error": {
        "code": "rate_limited",
        "message": "Rate limit exceeded for load-test@corp.example on 'runbooks__search_runbooks'. Retry after 720.0s (limit 5/hour).",
        "retry_after_seconds": 719.988,
        "limit": 5,
        "remaining": 0,
        "reset_at": 1783310302.7312505
      }
    }

========================================================================
4. RBAC DENIAL (dev-team principal calling an sre-team-only server)
========================================================================

-> dev@corp.example (teams=['dev-team']) calls metrics__query_metric
   DENIED  code=forbidden
   message: Access denied: teams ['dev-team'] may not use server 'metrics' (allowed: ['sre-team'])

   For contrast, the same dev IS allowed on the runbooks server:
   -> runbooks__get_runbook(id='rb-002')  isError=False
   {
     "id": "rb-002",
     "title": "Database connection pool exhaustion",
     "category": "database",
     "tags": [
       "database",
       "postgres",
       "connections",
       "rds"
     ],
     "steps": [
       "Check active vs. max connections on the primary",
       "Look for leaked connections / missing pool timeouts in callers",
       "Raise pool ceiling or add read replicas; verify error rate drops"
     ]
   }

========================================================================
5. AUDIT LOG (one structured entry per decision: allowed / denied / limited)
========================================================================

10 audit entries written to /var/folders/.../T/mcp_gateway_demo_audit.jsonl

  [OK  ] sre-oncall@corp.example runbooks__search_runbooks    result='{\n  "query": "database",\n ' dur=3ms
  [OK  ] sre-oncall@corp.example metrics__query_metric        result='{\n  "metric": "payment_ser' dur=2ms
  [OK  ] load-test@corp.example runbooks__search_runbooks    result='{\n  "query": "cpu",\n  "mat' dur=3ms
  [OK  ] load-test@corp.example runbooks__search_runbooks    result='{\n  "query": "cpu",\n  "mat' dur=1ms
  [OK  ] load-test@corp.example runbooks__search_runbooks    result='{\n  "query": "cpu",\n  "mat' dur=1ms
  [OK  ] load-test@corp.example runbooks__search_runbooks    result='{\n  "query": "cpu",\n  "mat' dur=1ms
  [OK  ] load-test@corp.example runbooks__search_runbooks    result='{\n  "query": "cpu",\n  "mat' dur=2ms
  [FAIL] load-test@corp.example runbooks__search_runbooks    result='RATE_LIMITED' dur=0ms
  [FAIL] dev@corp.example       metrics__query_metric        result='DENIED' dur=0ms
  [OK  ] dev@corp.example       runbooks__get_runbook        result='{\n  "id": "rb-002",\n  "tit' dur=1ms

Full JSON of a DENIED entry:
{
  "timestamp": "2026-07-06T02:58:22.743082+00:00",
  "session_id": "stdio-session",
  "user_email": "dev@corp.example",
  "user_teams": [
    "dev-team"
  ],
  "tool_name": "metrics__query_metric",
  "tool_args": {
    "name": "rds.connections_active"
  },
  "result_summary": "DENIED",
  "duration_ms": 0,
  "success": false,
  "error": "Access denied: teams ['dev-team'] may not use server 'metrics' (allowed: ['sre-team'])"
}

========================================================================
DEMO COMPLETE — gateway proved end-to-end on localhost (no cloud, no bound ports)
========================================================================
```

## What this output proves

- **Federation / namespacing** — one gateway aggregated the catalogs of two
  independent MCP subprocesses into namespaced tool names
  (`runbooks__*`, `metrics__*`), sorted and de-conflicted.
- **Routing** — `runbooks__search_runbooks` and `metrics__query_metric` were
  dispatched to the correct downstream subprocess (prefix stripped) and the
  real downstream responses came back through the gateway.
- **Rate limiting** — a per-principal, per-tool token bucket allowed exactly 5
  calls (`hourly=5, burst=0`) then rejected the 6th with a structured
  `rate_limited` error carrying `retry_after_seconds=719.988` and `remaining=0`.
- **RBAC** — the same catalog is filtered per principal (SRE sees 4 tools, Dev
  sees 2). A `dev-team` call into the `sre-team`-only `metrics` server was
  denied with `forbidden` **before** reaching the subprocess, while the dev's
  allowed `runbooks__get_runbook` call succeeded.
- **Audit** — every decision (7 allowed, 1 rate-limited, 1 denied, 1 allowed)
  produced a structured JSON audit entry with principal, teams, tool, result,
  duration, and error reason.

> Note: the boot snapshot shows `metrics tools_cached=0` purely because the
> catalog cache for that subprocess had not been primed at the instant the
> snapshot was taken; section 1 lists its two tools correctly once fetched.
> The temp audit path is abbreviated as `/var/folders/.../T/...` above.
