"""End-to-end local demo of the enterprise MCP gateway.

Boots the GatewayProxy from demo/gateway.demo.yaml, which registers TWO real
downstream MCP servers as stdio subprocesses (runbooks + metrics). Then, all
on localhost with no cloud and no network ports, it exercises the full path:

  1. Aggregate namespaced tool catalog (RBAC-filtered per principal)
  2. Routed tool calls through the gateway to each downstream
  3. Rate limiter: hammer one tool until the token bucket rejects with retry_after
  4. RBAC denial for a principal lacking the role
  5. The audit log the gateway wrote for every decision above

Run it:  ./demo/run_demo.sh      (or:  python demo/run_demo.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEMO_DIR.parent
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

# Make sure downstream stdio subprocesses ("command: python") resolve to THIS
# interpreter's venv, regardless of how the script was launched.
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

import structlog  # noqa: E402

from mcp_org.middleware.audit import AuditLogger  # noqa: E402
from mcp_org.middleware.auth import AuthenticatedUser  # noqa: E402
from mcp_org.proxy import GatewayProxy  # noqa: E402
from mcp.types import CallToolResult  # noqa: E402


def _configure_logging() -> None:
    """Route the gateway's structlog output to stderr and drop INFO noise so
    stdout is purely the demo narrative + audit trail."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )


def rule(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def result_text(result: CallToolResult) -> str:
    parts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts)


def error_of(result: CallToolResult) -> dict | None:
    if result.isError and result.structuredContent:
        return result.structuredContent.get("error")
    return None


def resolved_config() -> str:
    """Materialize demo/gateway.demo.yaml with absolute downstream paths."""
    template = (DEMO_DIR / "gateway.demo.yaml").read_text()
    text = template.replace("__DEMO_DIR__", str(DEMO_DIR))
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", prefix="gateway-resolved-", delete=False)
    tmp.write(text)
    tmp.close()
    return tmp.name


async def main() -> None:
    _configure_logging()

    audit_path = Path(tempfile.gettempdir()) / "mcp_gateway_demo_audit.jsonl"
    if audit_path.exists():
        audit_path.unlink()
    audit = AuditLogger(destination="file", log_file=str(audit_path))

    config_path = resolved_config()

    # Two principals: an SRE (full access) and a developer (runbooks only).
    sre = AuthenticatedUser(email="sre-oncall@corp.example", name="SRE Oncall", teams=["sre-team"], token="demo-sre")
    dev = AuthenticatedUser(email="dev@corp.example", name="App Developer", teams=["dev-team"], token="demo-dev")

    proxy = GatewayProxy.from_config(config_path, audit=audit)

    rule("GATEWAY BOOT")
    print(f"Gateway name        : {proxy.server.name}")
    print(f"Registered servers  : {[s.name for s in proxy.registry.list_servers()]}")
    print(f"Namespaces          : {proxy.registry.namespaces()}")
    print("Starting gateway (spawns downstream MCP servers as stdio subprocesses)...")
    await proxy.start()

    health = proxy.downstream_health()
    for name, snap in health.items():
        print(f"  downstream {name:9s} -> state={snap['state']} transport={snap['transport']} tools_cached={snap['cached_tools']}")

    try:
        # 1) Aggregate namespaced catalog, RBAC-filtered per principal ----------
        rule("1. AGGREGATE TOOL CATALOG (namespaced, RBAC-filtered)")
        sre_tools = await proxy.aggregate_tools(sre)
        print(f"\nAs SRE ({sre.email}, teams={sre.teams}) -> {len(sre_tools)} tools:")
        for t in sre_tools:
            print(f"  - {t.name:28s} {t.description}")

        dev_tools = await proxy.aggregate_tools(dev)
        print(f"\nAs Developer ({dev.email}, teams={dev.teams}) -> {len(dev_tools)} tools:")
        for t in dev_tools:
            print(f"  - {t.name:28s} {t.description}")
        print("  (metrics__* hidden: the metrics server is gated to sre-team)")

        # 2) Routed calls through the gateway to each downstream ---------------
        rule("2. ROUTED TOOL CALLS THROUGH THE GATEWAY")
        r1 = await proxy.dispatch(sre, "runbooks__search_runbooks", {"query": "database"})
        print("\n-> runbooks__search_runbooks(query='database')  [routed to runbooks subprocess]")
        print(result_text(r1))

        r2 = await proxy.dispatch(sre, "metrics__query_metric", {"name": "payment_service.p99_latency_ms", "window": "1h"})
        print("\n-> metrics__query_metric(name='payment_service.p99_latency_ms', window='1h')  [routed to metrics subprocess]")
        print(result_text(r2))

        # 3) Rate limiter: hammer one tool until the bucket rejects ------------
        rule("3. RATE LIMITER (token bucket: runbooks__search_runbooks = 5/hour, burst 0)")
        load_user = AuthenticatedUser(email="load-test@corp.example", name="Load Test", teams=["sre-team"], token="demo-load")
        print(f"\nHammering runbooks__search_runbooks as {load_user.email} ...")
        for i in range(1, 9):
            res = await proxy.dispatch(load_user, "runbooks__search_runbooks", {"query": "cpu"})
            err = error_of(res)
            if err and err["code"] == "rate_limited":
                print(f"  call #{i}: RATE LIMITED  code={err['code']}  "
                      f"retry_after={err['retry_after_seconds']}s  limit={err['limit']}/hour  remaining={err['remaining']}")
                print("  full structured error result the client receives:")
                print("    " + json.dumps(res.structuredContent, indent=2).replace("\n", "\n    "))
                break
            print(f"  call #{i}: allowed (isError={res.isError})")

        # 4) RBAC denial for a principal lacking the role ----------------------
        rule("4. RBAC DENIAL (dev-team principal calling an sre-team-only server)")
        denied = await proxy.dispatch(dev, "metrics__query_metric", {"name": "rds.connections_active"})
        err = error_of(denied)
        print(f"\n-> {dev.email} (teams={dev.teams}) calls metrics__query_metric")
        print(f"   DENIED  code={err['code']}")
        print(f"   message: {err['message']}")
        print("\n   For contrast, the same dev IS allowed on the runbooks server:")
        ok = await proxy.dispatch(dev, "runbooks__get_runbook", {"id": "rb-002"})
        print(f"   -> runbooks__get_runbook(id='rb-002')  isError={ok.isError}")
        print("   " + result_text(ok).replace("\n", "\n   "))

    finally:
        await proxy.stop()
        os.unlink(config_path)

    # 5) Audit trail the gateway wrote for every decision ----------------------
    rule("5. AUDIT LOG (one structured entry per decision: allowed / denied / limited)")
    lines = audit_path.read_text().splitlines()
    print(f"\n{len(lines)} audit entries written to {audit_path}\n")
    for line in lines:
        e = json.loads(line)
        status = "OK" if e["success"] else "FAIL"
        print(f"  [{status:4s}] {e['user_email']:22s} {e['tool_name']:28s} "
              f"result={e['result_summary'][:26]!r} dur={e['duration_ms']}ms")
    print("\nFull JSON of a DENIED entry:")
    for line in lines:
        e = json.loads(line)
        if not e["success"] and "denied" in (e.get("error") or "").lower():
            print(json.dumps(e, indent=2))
            break

    rule("DEMO COMPLETE — gateway proved end-to-end on localhost (no cloud, no bound ports)")


if __name__ == "__main__":
    asyncio.run(main())
