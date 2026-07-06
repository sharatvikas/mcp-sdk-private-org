"""Tool registry — discovers and loads MCP tool handlers from plugins."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp.types import Tool

logger = logging.getLogger(__name__)


class ToolHandler:
    """Base class for tool handlers registered in the gateway."""

    def get_tools(self) -> list[Tool]:
        """Return the list of MCP Tool definitions this handler provides."""
        raise NotImplementedError

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Execute the named tool with the given arguments."""
        raise NotImplementedError

    def handles(self, name: str) -> bool:
        """Return True if this handler owns the given tool name."""
        return name in {t.name for t in self.get_tools()}


class ToolRegistry:
    """
    Registry that aggregates ToolHandlers discovered from plugin directories
    or registered explicitly.

    Discovery order:
      1. Explicitly registered handlers (register())
      2. Built-in handlers from mcp_org.tools.*
      3. External plugin directories (TOOL_PLUGIN_DIRS env var, colon-separated)
    """

    def __init__(self) -> None:
        self._handlers: list[ToolHandler] = []
        self._tool_index: dict[str, ToolHandler] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, handler: ToolHandler) -> None:
        """Register a handler instance, indexing all its tools."""
        self._handlers.append(handler)
        for tool in handler.get_tools():
            if tool.name in self._tool_index:
                logger.warning(
                    "Tool %r already registered by %s — overriding with %s",
                    tool.name,
                    type(self._tool_index[tool.name]).__name__,
                    type(handler).__name__,
                )
            self._tool_index[tool.name] = handler
        logger.debug("Registered handler %s (%d tools)", type(handler).__name__, len(handler.get_tools()))

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def load_builtins(self) -> None:
        """
        Auto-discover handlers from the mcp_org.tools package.

        Any module in that package that defines a class ending in 'Handler'
        which is a subclass of ToolHandler will be instantiated and registered.
        """
        try:
            from mcp_org import tools as tools_pkg  # type: ignore[import]
        except ImportError:
            logger.debug("No mcp_org.tools package found, skipping builtin discovery")
            return

        pkg_path = Path(tools_pkg.__file__).parent  # type: ignore[arg-type]
        for py_file in sorted(pkg_path.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            module_name = f"mcp_org.tools.{py_file.stem}"
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                logger.warning("Failed to import %s: %s", module_name, exc)
                continue

            for attr_name in dir(module):
                if not attr_name.endswith("Handler"):
                    continue
                cls = getattr(module, attr_name)
                if (
                    isinstance(cls, type)
                    and issubclass(cls, ToolHandler)
                    and cls is not ToolHandler
                ):
                    try:
                        self.register(cls())
                    except Exception as exc:
                        logger.warning("Failed to instantiate %s: %s", attr_name, exc)

    def load_plugins(self, plugin_dirs: list[str] | None = None) -> None:
        """
        Load external plugins from directories.

        Each .py file in the directory is imported as a module.
        Any ToolHandler subclass found is registered automatically.

        Args:
            plugin_dirs: Explicit list of directories. Falls back to
                         TOOL_PLUGIN_DIRS env var (colon-separated).
        """
        dirs: list[str] = plugin_dirs or []
        if not dirs:
            env_dirs = os.environ.get("TOOL_PLUGIN_DIRS", "")
            dirs = [d for d in env_dirs.split(":") if d]

        for dir_str in dirs:
            plugin_path = Path(dir_str).expanduser().resolve()
            if not plugin_path.is_dir():
                logger.warning("Plugin dir %s does not exist, skipping", plugin_path)
                continue

            if str(plugin_path) not in sys.path:
                sys.path.insert(0, str(plugin_path))

            for py_file in sorted(plugin_path.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)  # type: ignore[union-attr]
                except Exception as exc:
                    logger.warning("Failed to load plugin %s: %s", py_file, exc)
                    continue

                for attr_name in dir(module):
                    if not attr_name.endswith("Handler"):
                        continue
                    cls = getattr(module, attr_name)
                    if (
                        isinstance(cls, type)
                        and issubclass(cls, ToolHandler)
                        and cls is not ToolHandler
                    ):
                        try:
                            self.register(cls())
                            logger.info("Loaded plugin handler %s from %s", attr_name, py_file)
                        except Exception as exc:
                            logger.warning("Failed to instantiate plugin %s: %s", attr_name, exc)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def all_tools(self) -> list[Tool]:
        """Return all registered tools across all handlers."""
        tools: list[Tool] = []
        for handler in self._handlers:
            tools.extend(handler.get_tools())
        return tools

    def get_handler(self, tool_name: str) -> ToolHandler | None:
        """Return the handler responsible for the given tool, or None."""
        return self._tool_index.get(tool_name)

    def tool_names(self) -> set[str]:
        """Return the set of all registered tool names."""
        return set(self._tool_index.keys())

    # ------------------------------------------------------------------
    # Async facade (used by the gateway's MCP handlers)
    # ------------------------------------------------------------------

    async def get_all_tools(self) -> list[Tool]:
        """Async facade over all_tools() for the gateway's list_tools handler."""
        return self.all_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch a tool call to the handler that owns *name*.

        Raises KeyError if no registered handler owns the tool.
        """
        handler = self.get_handler(name)
        if handler is None:
            raise KeyError(f"Unknown tool: {name!r} (registered: {sorted(self.tool_names()) or 'none'})")
        return await handler.call(name, arguments)

    def __len__(self) -> int:
        return len(self._tool_index)

    def __repr__(self) -> str:
        return f"ToolRegistry({len(self)} tools across {len(self._handlers)} handlers)"
