"""Postmortem draft tool — generates blameless postmortem documents using Claude."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from mcp.types import Tool

from mcp_org.registry.loader import ToolHandler


class PostmortemToolHandler(ToolHandler):
    """Generates blameless postmortem drafts from incident context using Claude."""

    def get_tools(self) -> list[Tool]:
        return [
            Tool(
                name="create_postmortem_draft",
                description=(
                    "Generate a blameless postmortem document from incident details. "
                    "Produces a structured markdown doc with timeline, root cause analysis, "
                    "contributing factors, and action items. Uses Claude claude-opus-4-6 for deep analysis."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["incident_title", "severity", "impact_summary", "timeline_events"],
                    "properties": {
                        "incident_title": {
                            "type": "string",
                            "description": "Short title for the incident (e.g. 'Payment service 503s - 2024-01-15')",
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["SEV1", "SEV2", "SEV3"],
                            "description": "Incident severity level",
                        },
                        "impact_summary": {
                            "type": "string",
                            "description": "What customers/systems were affected and for how long",
                        },
                        "timeline_events": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ordered list of events with timestamps (e.g. '14:03 - Alert fired: HighErrorRate on payment-service')",
                        },
                        "contributing_factors": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Known contributing factors (optional)",
                        },
                        "slack_thread": {
                            "type": "string",
                            "description": "Raw Slack thread text from the incident channel (optional but improves quality)",
                        },
                        "metrics_summary": {
                            "type": "string",
                            "description": "Key metrics during incident (error rates, latency, etc.)",
                        },
                    },
                },
            )
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name != "create_postmortem_draft":
            raise ValueError(f"Unknown tool: {name}")

        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

        timeline_text = "\n".join(f"  - {e}" for e in arguments["timeline_events"])
        factors_text = ""
        if arguments.get("contributing_factors"):
            factors_text = "\nKnown contributing factors:\n" + "\n".join(
                f"  - {f}" for f in arguments["contributing_factors"]
            )
        slack_text = ""
        if arguments.get("slack_thread"):
            slack_text = f"\nSlack incident thread (excerpt):\n{arguments['slack_thread'][:3000]}"
        metrics_text = ""
        if arguments.get("metrics_summary"):
            metrics_text = f"\nKey metrics during incident:\n{arguments['metrics_summary']}"

        prompt = f"""You are a Staff SRE writing a blameless postmortem. Generate a complete postmortem document.

Incident: {arguments['incident_title']}
Severity: {arguments['severity']}
Impact: {arguments['impact_summary']}

Timeline:
{timeline_text}
{factors_text}
{slack_text}
{metrics_text}

Write a complete blameless postmortem in Markdown with these sections:
1. **Executive Summary** (2-3 sentences, customer impact first)
2. **Timeline** (formatted table: Time | Event | Who)
3. **Root Cause** (the specific technical failure, not people)
4. **Contributing Factors** (systemic issues that allowed this to happen)
5. **What Went Well** (detection, response, communication wins)
6. **What Could Be Improved** (without blame)
7. **Action Items** (table: Item | Owner | Due Date | Priority)
8. **Lessons Learned** (systemic insights for the broader engineering org)

Tone: factual, blameless, specific. Action items must be concrete and assignable.
Today's date: {datetime.now().strftime('%Y-%m-%d')}"""

        resp = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        return resp.content[0].text
