"""Audit logging — structured JSON log of every MCP tool invocation."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger()


@dataclass
class AuditEntry:
    timestamp: str
    session_id: str
    user_email: str
    user_teams: list[str]
    tool_name: str
    tool_args: dict[str, Any]
    result_summary: str
    duration_ms: int
    success: bool
    error: str | None = None


class AuditLogger:
    """Write structured audit logs for all MCP tool calls."""

    def __init__(
        self,
        destination: str = "stdout",
        log_file: str | None = None,
        s3_bucket: str | None = None,
    ) -> None:
        """
        destination: "stdout" | "file" | "s3" | "cloudwatch"
        """
        self._destination = destination
        self._log_file = log_file or os.environ.get("MCP_AUDIT_LOG_FILE")
        self._s3_bucket = s3_bucket or os.environ.get("MCP_AUDIT_S3_BUCKET")

    def log_call(
        self,
        session_id: str,
        user_email: str,
        user_teams: list[str],
        tool_name: str,
        tool_args: dict,
        result: str,
        duration_ms: int,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        """Record a tool invocation."""
        # Redact sensitive argument values
        safe_args = _redact_sensitive(tool_args)

        entry = AuditEntry(
            timestamp=datetime.now(UTC).isoformat(),
            session_id=session_id,
            user_email=user_email,
            user_teams=user_teams,
            tool_name=tool_name,
            tool_args=safe_args,
            result_summary=result[:500] if result else "",  # Truncate long results
            duration_ms=duration_ms,
            success=success,
            error=error,
        )

        log.info(
            "tool_call",
            tool=tool_name,
            user=user_email,
            duration_ms=duration_ms,
            success=success,
        )

        entry_json = json.dumps(asdict(entry))

        match self._destination:
            case "stdout":
                print(entry_json)
            case "file":
                self._write_to_file(entry_json)
            case "s3":
                self._write_to_s3(entry_json, entry.timestamp)
            case "cloudwatch":
                self._write_to_cloudwatch(entry_json)

    def _write_to_file(self, entry_json: str) -> None:
        if not self._log_file:
            return
        with open(self._log_file, "a") as f:
            f.write(entry_json + "\n")

    def _write_to_s3(self, entry_json: str, timestamp: str) -> None:
        if not self._s3_bucket:
            return
        try:
            import boto3
            s3 = boto3.client("s3")
            date_prefix = timestamp[:10]  # YYYY-MM-DD
            hour = timestamp[11:13]
            key = f"mcp-audit/{date_prefix}/{hour}/{int(time.time() * 1000)}.json"
            s3.put_object(
                Bucket=self._s3_bucket,
                Key=key,
                Body=entry_json.encode(),
                ContentType="application/json",
            )
        except Exception as e:
            log.error("s3_audit_write_failed", error=str(e))

    def _write_to_cloudwatch(self, entry_json: str) -> None:
        try:
            import boto3
            cw = boto3.client("logs")
            log_group = os.environ.get("MCP_AUDIT_LOG_GROUP", "/mcp-server/audit")
            log_stream = datetime.now(UTC).strftime("%Y/%m/%d")

            cw.put_log_events(
                logGroupName=log_group,
                logStreamName=log_stream,
                logEvents=[{
                    "timestamp": int(time.time() * 1000),
                    "message": entry_json,
                }],
            )
        except Exception as e:
            log.error("cloudwatch_audit_write_failed", error=str(e))


def _redact_sensitive(args: dict) -> dict:
    """Replace sensitive argument values with [REDACTED]."""
    sensitive_keys = {
        "password", "secret", "token", "api_key", "private_key",
        "access_key", "secret_key", "credential", "auth",
    }
    result = {}
    for k, v in args.items():
        if any(s in k.lower() for s in sensitive_keys):
            result[k] = "[REDACTED]"
        elif isinstance(v, dict):
            result[k] = _redact_sensitive(v)
        else:
            result[k] = v
    return result
