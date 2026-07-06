"""Runbook search tool — semantic search over runbooks using Claude embeddings."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from mcp.types import Tool

from mcp_org.registry.loader import ToolHandler


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class RunbookToolHandler(ToolHandler):
    """Semantic runbook search using text-embedding-3-small via Anthropic-compatible embeddings."""

    def get_tools(self) -> list[Tool]:
        return [
            Tool(
                name="search_runbooks",
                description=(
                    "Semantic search over the runbook library. Returns the most relevant "
                    "runbooks for a given symptom, alert name, or error message. "
                    "Uses embeddings for fuzzy matching — works even with paraphrasing."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Symptom, alert name, or error to search for (e.g. 'OOMKilled pod in payment-service', 'high p99 latency')",
                        },
                        "top_k": {
                            "type": "integer",
                            "default": 3,
                            "description": "Number of results to return (default 3)",
                        },
                    },
                },
            ),
            Tool(
                name="get_runbook",
                description="Fetch the full content of a specific runbook by name.",
                inputSchema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Runbook filename (without .md extension)",
                        },
                    },
                },
            ),
            Tool(
                name="list_runbook_categories",
                description="List all available runbooks grouped by category prefix.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "search_runbooks":
            return await self._search(arguments["query"], arguments.get("top_k", 3))
        if name == "get_runbook":
            return self._get(arguments["name"])
        if name == "list_runbook_categories":
            return self._list()
        raise ValueError(f"Unknown tool: {name}")

    # ── Implementation ─────────────────────────────────────────────────────

    def _runbooks_dir(self) -> Path:
        return Path(os.environ.get("RUNBOOKS_DIR", Path.home() / "runbooks"))

    def _index_path(self) -> Path:
        return self._runbooks_dir() / ".embedding-index.json"

    def _list_runbook_files(self) -> list[Path]:
        d = self._runbooks_dir()
        if not d.exists():
            return []
        return sorted(d.glob("*.md"))

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """Get embeddings using OpenAI-compatible endpoint or fall back to TF-IDF approximation."""
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    json={"model": "text-embedding-3-small", "input": texts},
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
                return [item["embedding"] for item in data["data"]]

        # Fallback: simple bag-of-words TF vector (no external API needed)
        return self._bow_vectors(texts)

    def _bow_vectors(self, texts: list[str]) -> list[list[float]]:
        """Bag-of-words fallback when no embedding API is available."""
        import re
        vocab: dict[str, int] = {}
        tokenized = []
        for text in texts:
            tokens = re.findall(r'\b\w+\b', text.lower())
            tokenized.append(tokens)
            for t in tokens:
                if t not in vocab:
                    vocab[t] = len(vocab)

        vectors = []
        for tokens in tokenized:
            vec = [0.0] * len(vocab)
            for t in tokens:
                vec[vocab[t]] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vectors.append([x / norm for x in vec])
        return vectors

    async def _build_index(self) -> dict:
        """Build or load embedding index for all runbooks."""
        index_path = self._index_path()
        files = self._list_runbook_files()

        if not files:
            return {}

        # Load existing index and check freshness
        existing: dict = {}
        if index_path.exists():
            with open(index_path) as f:
                existing = json.load(f)

        # Determine which files need re-embedding
        to_embed = []
        for f in files:
            mtime = str(f.stat().st_mtime)
            if f.name not in existing or existing[f.name].get("mtime") != mtime:
                to_embed.append(f)

        if to_embed:
            # Read and chunk runbook content (first 2000 chars for embedding)
            contents = []
            for f in to_embed:
                text = f.read_text(encoding="utf-8", errors="replace")[:2000]
                contents.append(text)

            embeddings = await self._embed(contents)
            for f, embedding in zip(to_embed, embeddings):
                existing[f.name] = {
                    "mtime": str(f.stat().st_mtime),
                    "embedding": embedding,
                    "title": self._extract_title(f),
                }

            with open(index_path, "w") as out:
                json.dump(existing, out)

        return existing

    def _extract_title(self, path: Path) -> str:
        try:
            for line in path.read_text().splitlines():
                if line.startswith("# "):
                    return line[2:].strip()
        except Exception:
            pass
        return path.stem.replace("-", " ").title()

    async def _search(self, query: str, top_k: int) -> str:
        index = await self._build_index()
        if not index:
            return "No runbooks found. Set RUNBOOKS_DIR to your runbooks directory."

        query_vec = (await self._embed([query]))[0]

        scores = []
        for filename, entry in index.items():
            sim = _cosine_similarity(query_vec, entry["embedding"])
            scores.append((sim, filename, entry["title"]))

        scores.sort(reverse=True)
        top = scores[:top_k]

        lines = [f"Top {len(top)} runbook matches for: '{query}'\n"]
        for i, (score, filename, title) in enumerate(top, 1):
            pct = int(score * 100)
            lines.append(f"{i}. **{title}** (`{filename}`) — {pct}% match")
            # Show first 200 chars of content as preview
            path = self._runbooks_dir() / filename
            if path.exists():
                preview = path.read_text()[:200].replace("\n", " ").strip()
                lines.append(f"   > {preview}...")

        lines.append("\nUse `get_runbook` with the filename (without .md) to fetch full content.")
        return "\n".join(lines)

    def _get(self, name: str) -> str:
        d = self._runbooks_dir()
        path = d / f"{name}.md"
        if not path.exists():
            path = d / name
        if not path.exists():
            return f"Runbook not found: {name}"
        return path.read_text(encoding="utf-8")

    def _list(self) -> str:
        files = self._list_runbook_files()
        if not files:
            return "No runbooks found. Set RUNBOOKS_DIR to your runbooks directory."

        categories: dict[str, list[str]] = {}
        for f in files:
            parts = f.stem.split("-", 1)
            cat = parts[0].title() if len(parts) > 1 else "General"
            categories.setdefault(cat, []).append(f.stem)

        lines = ["Available runbooks by category:\n"]
        for cat, names in sorted(categories.items()):
            lines.append(f"**{cat}**")
            for name in sorted(names):
                lines.append(f"  - `{name}`")
        return "\n".join(lines)
