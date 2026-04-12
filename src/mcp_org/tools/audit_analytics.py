"""Audit log analytics tool — surface MCP gateway usage patterns.

Provides three tools for analyzing the structured audit logs written by
AuditLogger: top tools by invocation count, per-user activity summary,
and error rate analysis with failure pattern detection.

Reads from the audit log file (MCP_AUDIT_LOG_FILE) or accepts an
explicit file path argument.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


_AUDIT_LOG_FILE = os.environ.get("MCP_AUDIT_LOG_FILE", "/var/log/mcp-gateway/audit.jsonl")


def _load_entries(
    log_file: str,
    hours_back: int = 24,
) -> list[dict]:
    """Load audit log entries from a JSONL file within the time window."""
    path = Path(log_file)
    if not path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    entries = []

    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts_str = entry.get("timestamp", "")
                ts = datetime.fromisoformat(ts_str.rstrip("Z")).replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    entries.append(entry)
            except (json.JSONDecodeError, ValueError):
                continue

    return entries


async def get_tool_usage_stats(arguments: dict[str, Any]) -> str:
    """
    Get usage statistics for MCP tools over the past N hours.

    Arguments:
      hours_back (int): Look-back window (default 24)
      log_file (str): Path to audit log file (default: MCP_AUDIT_LOG_FILE env)
      top_n (int): Number of top tools to return (default 10)
      include_errors (bool): Include error breakdown (default true)

    Returns JSON with:
      - total_calls: total invocations
      - unique_users: distinct users
      - top_tools: ranked list by call count with success/error breakdown
      - error_rate: overall error rate
      - busiest_hour: hour with most activity
    """
    hours_back = int(arguments.get("hours_back", 24))
    log_file = arguments.get("log_file", _AUDIT_LOG_FILE)
    top_n = int(arguments.get("top_n", 10))
    include_errors = bool(arguments.get("include_errors", True))

    entries = _load_entries(log_file, hours_back)

    if not entries:
        return json.dumps({
            "message": f"No audit log entries found in the past {hours_back}h",
            "log_file": log_file,
            "total_calls": 0,
        }, indent=2)

    tool_calls = Counter(e["tool_name"] for e in entries)
    tool_errors = Counter(e["tool_name"] for e in entries if not e.get("success", True))
    tool_durations: dict[str, list[int]] = defaultdict(list)
    for e in entries:
        tool_durations[e["tool_name"]].append(e.get("duration_ms", 0))

    unique_users = len(set(e.get("user_email", "") for e in entries))

    total_calls = len(entries)
    total_errors = sum(1 for e in entries if not e.get("success", True))

    # Hourly breakdown
    hour_counts: Counter = Counter()
    for e in entries:
        try:
            ts = datetime.fromisoformat(e["timestamp"].rstrip("Z"))
            hour_counts[ts.strftime("%Y-%m-%d %H:00")] += 1
        except (ValueError, KeyError):
            pass

    busiest_hour = hour_counts.most_common(1)[0] if hour_counts else ("N/A", 0)

    top_tools = []
    for tool, count in tool_calls.most_common(top_n):
        durations = tool_durations[tool]
        avg_ms = int(sum(durations) / len(durations)) if durations else 0
        entry = {
            "tool": tool,
            "calls": count,
            "call_pct": round(count / total_calls * 100, 1),
            "avg_duration_ms": avg_ms,
        }
        if include_errors:
            errors = tool_errors.get(tool, 0)
            entry["errors"] = errors
            entry["error_rate_pct"] = round(errors / count * 100, 1) if count > 0 else 0
        top_tools.append(entry)

    return json.dumps({
        "window_hours": hours_back,
        "total_calls": total_calls,
        "unique_users": unique_users,
        "error_rate_pct": round(total_errors / total_calls * 100, 1) if total_calls > 0 else 0,
        "busiest_hour": busiest_hour[0],
        "busiest_hour_calls": busiest_hour[1],
        "top_tools": top_tools,
    }, indent=2)


async def get_user_activity_report(arguments: dict[str, Any]) -> str:
    """
    Get per-user activity summary over the past N hours.

    Arguments:
      hours_back (int): Look-back window (default 24)
      log_file (str): Path to audit log file
      user_email (str): Filter to a specific user (optional)
      top_n (int): Top N most active users (default 10)

    Returns JSON with per-user call counts, most-used tools,
    error rates, and team membership.
    """
    hours_back = int(arguments.get("hours_back", 24))
    log_file = arguments.get("log_file", _AUDIT_LOG_FILE)
    user_filter = arguments.get("user_email", "")
    top_n = int(arguments.get("top_n", 10))

    entries = _load_entries(log_file, hours_back)
    if user_filter:
        entries = [e for e in entries if e.get("user_email", "") == user_filter]

    if not entries:
        return json.dumps({
            "message": f"No entries found for {'user ' + user_filter if user_filter else 'any user'} in {hours_back}h",
            "total_calls": 0,
        }, indent=2)

    # Aggregate by user
    user_calls: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "errors": 0, "tools": Counter(), "teams": set(),
        "first_seen": None, "last_seen": None,
    })

    for e in entries:
        email = e.get("user_email", "unknown")
        ud = user_calls[email]
        ud["calls"] += 1
        if not e.get("success", True):
            ud["errors"] += 1
        ud["tools"][e.get("tool_name", "")] += 1
        for team in e.get("user_teams", []):
            ud["teams"].add(team)
        ts = e.get("timestamp", "")
        if not ud["first_seen"] or ts < ud["first_seen"]:
            ud["first_seen"] = ts
        if not ud["last_seen"] or ts > ud["last_seen"]:
            ud["last_seen"] = ts

    # Sort by call count
    sorted_users = sorted(user_calls.items(), key=lambda x: x[1]["calls"], reverse=True)

    results = []
    for email, ud in sorted_users[:top_n]:
        top_tool = ud["tools"].most_common(1)[0] if ud["tools"] else ("N/A", 0)
        results.append({
            "user": email,
            "teams": sorted(ud["teams"]),
            "total_calls": ud["calls"],
            "errors": ud["errors"],
            "error_rate_pct": round(ud["errors"] / ud["calls"] * 100, 1) if ud["calls"] > 0 else 0,
            "most_used_tool": top_tool[0],
            "most_used_tool_calls": top_tool[1],
            "unique_tools_used": len(ud["tools"]),
            "first_call": ud["first_seen"],
            "last_call": ud["last_seen"],
        })

    return json.dumps({
        "window_hours": hours_back,
        "user_filter": user_filter or "all",
        "total_users": len(user_calls),
        "users": results,
    }, indent=2)


async def get_error_analysis(arguments: dict[str, Any]) -> str:
    """
    Analyze error patterns in the audit log.

    Arguments:
      hours_back (int): Look-back window (default 24)
      log_file (str): Path to audit log file
      tool_name (str): Filter to a specific tool (optional)

    Returns JSON with:
      - error_rate: overall error rate
      - top_failing_tools: tools with highest error rates
      - common_errors: most frequent error messages
      - error_spikes: time windows with elevated error rates
    """
    hours_back = int(arguments.get("hours_back", 24))
    log_file = arguments.get("log_file", _AUDIT_LOG_FILE)
    tool_filter = arguments.get("tool_name", "")

    entries = _load_entries(log_file, hours_back)
    if tool_filter:
        entries = [e for e in entries if e.get("tool_name", "") == tool_filter]

    if not entries:
        return json.dumps({"message": f"No audit entries found in {hours_back}h", "total_calls": 0}, indent=2)

    error_entries = [e for e in entries if not e.get("success", True)]
    total = len(entries)
    errors = len(error_entries)

    # Per-tool error rates
    tool_stats: dict[str, dict] = defaultdict(lambda: {"calls": 0, "errors": 0})
    for e in entries:
        tool = e.get("tool_name", "unknown")
        tool_stats[tool]["calls"] += 1
        if not e.get("success", True):
            tool_stats[tool]["errors"] += 1

    top_failing = sorted(
        [
            {
                "tool": tool,
                "calls": s["calls"],
                "errors": s["errors"],
                "error_rate_pct": round(s["errors"] / s["calls"] * 100, 1),
            }
            for tool, s in tool_stats.items()
            if s["errors"] > 0
        ],
        key=lambda x: x["error_rate_pct"],
        reverse=True,
    )[:10]

    # Most common error messages
    error_messages = Counter(
        e.get("error", "unknown error")
        for e in error_entries
        if e.get("error")
    )
    common_errors = [
        {"message": msg[:200], "count": count}
        for msg, count in error_messages.most_common(10)
    ]

    # Hourly error rates to detect spikes
    hourly: dict[str, dict] = defaultdict(lambda: {"calls": 0, "errors": 0})
    for e in entries:
        try:
            ts = datetime.fromisoformat(e["timestamp"].rstrip("Z"))
            hour = ts.strftime("%Y-%m-%d %H:00")
            hourly[hour]["calls"] += 1
            if not e.get("success", True):
                hourly[hour]["errors"] += 1
        except (ValueError, KeyError):
            pass

    avg_error_rate = errors / total if total > 0 else 0
    error_spikes = [
        {
            "hour": hour,
            "calls": h["calls"],
            "errors": h["errors"],
            "error_rate_pct": round(h["errors"] / h["calls"] * 100, 1) if h["calls"] > 0 else 0,
        }
        for hour, h in hourly.items()
        if h["calls"] > 0 and (h["errors"] / h["calls"]) > avg_error_rate * 2
    ]
    error_spikes.sort(key=lambda x: x["error_rate_pct"], reverse=True)

    return json.dumps({
        "window_hours": hours_back,
        "tool_filter": tool_filter or "all",
        "total_calls": total,
        "total_errors": errors,
        "overall_error_rate_pct": round(errors / total * 100, 1) if total > 0 else 0,
        "top_failing_tools": top_failing,
        "common_errors": common_errors,
        "error_spikes": error_spikes[:5],
        "recommendation": (
            f"Highest error rate: {top_failing[0]['tool']} at {top_failing[0]['error_rate_pct']}%"
            if top_failing else "No errors detected"
        ),
    }, indent=2)


# Tool metadata for MCP registry auto-discovery
TOOL_DEFINITIONS = [
    {
        "name": "get_tool_usage_stats",
        "fn": get_tool_usage_stats,
        "description": (
            "Get usage statistics for MCP tools: call counts, error rates, avg latency, "
            "and busiest time windows. Use this for capacity planning or to identify "
            "which tools are being used most and where errors occur."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours_back": {"type": "integer", "description": "Look-back window in hours", "default": 24},
                "log_file": {"type": "string", "description": "Path to audit log JSONL file"},
                "top_n": {"type": "integer", "description": "Top N tools to return", "default": 10},
                "include_errors": {"type": "boolean", "description": "Include error breakdown", "default": True},
            },
            "required": [],
        },
    },
    {
        "name": "get_user_activity_report",
        "fn": get_user_activity_report,
        "description": (
            "Get per-user activity summary: which users are calling which tools, "
            "error rates per user, and team-level usage patterns. "
            "Useful for access reviews, cost allocation, and anomaly detection."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours_back": {"type": "integer", "description": "Look-back window in hours", "default": 24},
                "log_file": {"type": "string", "description": "Path to audit log JSONL file"},
                "user_email": {"type": "string", "description": "Filter to a specific user email"},
                "top_n": {"type": "integer", "description": "Top N most active users", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "get_error_analysis",
        "fn": get_error_analysis,
        "description": (
            "Analyze error patterns in the MCP audit log. "
            "Identifies top failing tools, common error messages, and time-based error spikes. "
            "Use during incident investigation when MCP tools are misbehaving."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours_back": {"type": "integer", "description": "Look-back window in hours", "default": 24},
                "log_file": {"type": "string", "description": "Path to audit log JSONL file"},
                "tool_name": {"type": "string", "description": "Filter to a specific tool name"},
            },
            "required": [],
        },
    },
]
