"""Downstream MCP server registry for the federation gateway.

Describes the fleet of MCP servers that sit *behind* the gateway proxy:
what they are called, how to reach them (stdio subprocess, SSE, or
streamable HTTP), which tool namespace they own, and which teams are
allowed to touch them.

Sources:
  - YAML or JSON config file (``ServerRegistry.from_file``)
  - Plain dicts (``ServerRegistry.from_dict``) — e.g. from a config service
  - Programmatic API (``register`` / ``deregister``)

Every load path runs full validation and raises ``ServerRegistryError``
with *all* problems found (not just the first) so operators can fix a
config in one pass.

Hot reload: when constructed from a file, ``maybe_reload()`` re-reads the
file if its content changed (mtime + SHA-256 check) and atomically swaps
the spec set. Subscribers registered via ``subscribe()`` are notified with
the sets of added / removed / changed server names so the gateway can
start or stop downstream connections accordingly.

Config example (YAML)::

    servers:
      - name: runbooks
        namespace: runbooks
        transport: stdio
        command: python
        args: ["-m", "runbooks_mcp"]
        env: { LOG_LEVEL: info }
        allowed_teams: [sre-team, dev-team]
      - name: deploys
        namespace: deploy
        transport: sse
        url: http://deploy-mcp.internal:8080/sse
        allowed_teams: [sre-team]
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")

# Names and namespaces end up embedded in tool names exposed to MCP
# clients, which commonly restrict tool names to [a-zA-Z0-9_-]{1,128}.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")


class ServerRegistryError(ValueError):
    """Raised when a server registry config fails validation.

    Carries the full list of validation errors in ``.errors``.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        summary = "\n  - ".join(errors)
        super().__init__(f"Server registry validation failed ({len(errors)} error(s)):\n  - {summary}")


@dataclass(frozen=True)
class DownstreamServerSpec:
    """Declarative description of one downstream MCP server."""

    name: str
    namespace: str
    transport: str = "stdio"
    # stdio transport
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # sse / streamable-http transport
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    # governance
    allowed_teams: tuple[str, ...] = ("*",)
    enabled: bool = True
    description: str = ""
    # operational tuning
    request_timeout_seconds: float = 30.0
    health_check_interval_seconds: float = 30.0
    ping_timeout_seconds: float = 5.0
    connect_timeout_seconds: float = 15.0

    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, position: int | None = None) -> tuple[DownstreamServerSpec | None, list[str]]:
        """Build a spec from a raw dict, returning (spec, errors).

        Unknown keys are rejected so config typos fail loudly instead of
        silently disabling a setting.
        """
        where = f"servers[{position}]" if position is not None else "server"
        errors: list[str] = []

        if not isinstance(data, Mapping):
            return None, [f"{where}: expected a mapping, got {type(data).__name__}"]

        known = {f.name for f in fields(cls)}
        unknown = set(data.keys()) - known
        if unknown:
            errors.append(f"{where}: unknown field(s): {', '.join(sorted(unknown))} (valid: {', '.join(sorted(known))})")

        kwargs: dict[str, Any] = {k: v for k, v in data.items() if k in known}
        # Normalise sequence fields to tuples so specs are immutable/comparable
        if "args" in kwargs:
            if not isinstance(kwargs["args"], (list, tuple)):
                errors.append(f"{where}: 'args' must be a list of strings")
                kwargs.pop("args")
            else:
                kwargs["args"] = tuple(str(a) for a in kwargs["args"])
        if "allowed_teams" in kwargs:
            if not isinstance(kwargs["allowed_teams"], (list, tuple)) or not kwargs["allowed_teams"]:
                errors.append(f"{where}: 'allowed_teams' must be a non-empty list of team names")
                kwargs.pop("allowed_teams")
            else:
                kwargs["allowed_teams"] = tuple(str(t) for t in kwargs["allowed_teams"])
        for map_key in ("env", "headers"):
            if map_key in kwargs and not isinstance(kwargs[map_key], Mapping):
                errors.append(f"{where}: '{map_key}' must be a mapping of string to string")
                kwargs.pop(map_key)

        if "name" not in kwargs:
            errors.append(f"{where}: 'name' is required")
        if "namespace" not in kwargs:
            errors.append(f"{where}: 'namespace' is required")
        if errors and ("name" not in kwargs or "namespace" not in kwargs):
            return None, errors

        try:
            spec = cls(**kwargs)
        except TypeError as exc:
            return None, errors + [f"{where}: {exc}"]

        errors.extend(spec.validate(where=where))
        return (spec if not errors else None), errors

    def validate(self, *, where: str | None = None) -> list[str]:
        """Return a list of human-readable validation errors (empty if valid)."""
        prefix = where or f"server '{self.name}'"
        errors: list[str] = []

        if not _NAME_RE.match(self.name or ""):
            errors.append(f"{prefix}: invalid name {self.name!r} (must match {_NAME_RE.pattern})")
        if not _NAMESPACE_RE.match(self.namespace or ""):
            errors.append(f"{prefix}: invalid namespace {self.namespace!r} (must match {_NAMESPACE_RE.pattern})")

        if self.transport not in VALID_TRANSPORTS:
            errors.append(f"{prefix}: invalid transport {self.transport!r} (must be one of {', '.join(VALID_TRANSPORTS)})")
        elif self.transport == "stdio":
            if not self.command:
                errors.append(f"{prefix}: stdio transport requires 'command'")
            if self.url:
                errors.append(f"{prefix}: 'url' is not valid for stdio transport")
        else:  # sse / streamable-http
            if not self.url:
                errors.append(f"{prefix}: {self.transport} transport requires 'url'")
            elif not re.match(r"^https?://", self.url):
                errors.append(f"{prefix}: url {self.url!r} must start with http:// or https://")
            if self.command:
                errors.append(f"{prefix}: 'command' is not valid for {self.transport} transport")

        for attr in ("request_timeout_seconds", "health_check_interval_seconds", "ping_timeout_seconds", "connect_timeout_seconds"):
            value = getattr(self, attr)
            if not isinstance(value, (int, float)) or value <= 0:
                errors.append(f"{prefix}: '{attr}' must be a positive number, got {value!r}")

        if not self.allowed_teams:
            errors.append(f"{prefix}: 'allowed_teams' must not be empty (use ['*'] to allow all teams)")

        return errors

    def allows_teams(self, teams: list[str] | tuple[str, ...]) -> bool:
        """True if a principal with the given team memberships may use this server."""
        if "*" in self.allowed_teams:
            return True
        return bool(set(self.allowed_teams) & set(teams))


# Callback signature: (added_names, removed_names, changed_names)
RegistryChangeCallback = Callable[[set[str], set[str], set[str]], None]


class ServerRegistry:
    """Thread-safe registry of downstream MCP servers.

    Supports file-based loading with hot reload plus a programmatic API.
    """

    def __init__(self, specs: list[DownstreamServerSpec] | None = None, *, source_path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._specs: dict[str, DownstreamServerSpec] = {}
        self._source_path = Path(source_path) if source_path else None
        self._source_digest: str | None = None
        self._source_mtime: float | None = None
        self._callbacks: list[RegistryChangeCallback] = []
        if specs:
            errors = _validate_spec_set(specs)
            if errors:
                raise ServerRegistryError(errors)
            self._specs = {s.name: s for s in specs}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: str | Path) -> ServerRegistry:
        """Load and validate a registry from a YAML or JSON file."""
        p = Path(path)
        raw = _read_config_file(p)
        specs = _parse_specs(raw)
        registry = cls(specs, source_path=p)
        registry._source_digest = _digest(p)
        registry._source_mtime = p.stat().st_mtime
        log.info("server_registry.loaded", path=str(p), servers=len(specs))
        return registry

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | list) -> ServerRegistry:
        """Build a registry from an already-parsed config structure."""
        return cls(_parse_specs(data))

    # ------------------------------------------------------------------
    # Programmatic API
    # ------------------------------------------------------------------

    def register(self, spec: DownstreamServerSpec) -> None:
        """Add or replace a server spec, revalidating the whole set."""
        with self._lock:
            candidate = {**self._specs, spec.name: spec}
            errors = _validate_spec_set(list(candidate.values()))
            if errors:
                raise ServerRegistryError(errors)
            is_new = spec.name not in self._specs
            self._specs = candidate
        log.info("server_registry.registered", server=spec.name, namespace=spec.namespace, new=is_new)
        self._notify({spec.name} if is_new else set(), set(), set() if is_new else {spec.name})

    def deregister(self, name: str) -> DownstreamServerSpec:
        """Remove a server by name. Raises KeyError if not present."""
        with self._lock:
            if name not in self._specs:
                raise KeyError(f"No downstream server named {name!r} is registered")
            spec = self._specs.pop(name)
        log.info("server_registry.deregistered", server=name)
        self._notify(set(), {name}, set())
        return spec

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> DownstreamServerSpec | None:
        with self._lock:
            return self._specs.get(name)

    def get_by_namespace(self, namespace: str) -> DownstreamServerSpec | None:
        with self._lock:
            for spec in self._specs.values():
                if spec.namespace == namespace:
                    return spec
        return None

    def list_servers(self, *, enabled_only: bool = False) -> list[DownstreamServerSpec]:
        with self._lock:
            specs = list(self._specs.values())
        if enabled_only:
            specs = [s for s in specs if s.enabled]
        return specs

    def namespaces(self) -> list[str]:
        with self._lock:
            return sorted(s.namespace for s in self._specs.values())

    def resolve_tool(self, full_name: str, *, separator: str = "__") -> tuple[DownstreamServerSpec, str]:
        """Split a namespaced tool name into (server spec, local tool name).

        Raises KeyError with an actionable message when the name has no
        namespace prefix or the prefix matches no registered server.
        """
        namespace, sep, local_name = full_name.partition(separator)
        if not sep or not local_name:
            raise KeyError(
                f"Tool name {full_name!r} has no namespace prefix "
                f"(expected '<namespace>{separator}<tool>'; known namespaces: {', '.join(self.namespaces()) or 'none'})"
            )
        spec = self.get_by_namespace(namespace)
        if spec is None:
            raise KeyError(
                f"No downstream server owns namespace {namespace!r} "
                f"(known namespaces: {', '.join(self.namespaces()) or 'none'})"
            )
        return spec, local_name

    def __len__(self) -> int:
        with self._lock:
            return len(self._specs)

    def __repr__(self) -> str:
        return f"ServerRegistry({len(self)} servers, source={self._source_path})"

    # ------------------------------------------------------------------
    # Hot reload
    # ------------------------------------------------------------------

    def subscribe(self, callback: RegistryChangeCallback) -> None:
        """Register a callback invoked with (added, removed, changed) names on any change."""
        self._callbacks.append(callback)

    def reload(self) -> tuple[set[str], set[str], set[str]]:
        """Force a re-read of the source file. Returns (added, removed, changed).

        Raises RuntimeError if the registry was not loaded from a file, and
        ServerRegistryError if the new file content is invalid (in which
        case the previous spec set stays active).
        """
        if self._source_path is None:
            raise RuntimeError("This registry was not loaded from a file; nothing to reload")

        raw = _read_config_file(self._source_path)
        specs = _parse_specs(raw)
        errors = _validate_spec_set(specs)
        if errors:
            raise ServerRegistryError(errors)

        new_specs = {s.name: s for s in specs}
        with self._lock:
            old_specs = self._specs
            added = set(new_specs) - set(old_specs)
            removed = set(old_specs) - set(new_specs)
            changed = {n for n in set(new_specs) & set(old_specs) if new_specs[n] != old_specs[n]}
            self._specs = new_specs
            self._source_digest = _digest(self._source_path)
            self._source_mtime = self._source_path.stat().st_mtime

        if added or removed or changed:
            log.info("server_registry.reloaded", added=sorted(added), removed=sorted(removed), changed=sorted(changed))
            self._notify(added, removed, changed)
        return added, removed, changed

    def maybe_reload(self) -> bool:
        """Reload only if the source file content changed. Returns True if it did.

        A failed reload (invalid new config) is logged and swallowed so a
        bad config push cannot take down a running gateway; the previous
        good config stays active.
        """
        if self._source_path is None:
            return False
        try:
            stat = self._source_path.stat()
        except OSError as exc:
            log.warning("server_registry.reload_stat_failed", path=str(self._source_path), error=str(exc))
            return False

        if self._source_mtime is not None and stat.st_mtime == self._source_mtime:
            return False
        digest = _digest(self._source_path)
        if digest == self._source_digest:
            # mtime changed but content identical (e.g. touch) — remember new mtime
            with self._lock:
                self._source_mtime = stat.st_mtime
            return False

        try:
            added, removed, changed = self.reload()
        except (ServerRegistryError, OSError, ValueError) as exc:
            log.error("server_registry.hot_reload_failed", path=str(self._source_path), error=str(exc))
            return False
        return bool(added or removed or changed)

    def _notify(self, added: set[str], removed: set[str], changed: set[str]) -> None:
        for callback in self._callbacks:
            try:
                callback(added, removed, changed)
            except Exception as exc:  # never let a subscriber break the registry
                log.error("server_registry.callback_failed", callback=repr(callback), error=str(exc))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _read_config_file(path: Path) -> Any:
    if not path.is_file():
        raise ServerRegistryError([f"Config file not found: {path}"])
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            import yaml

            return yaml.safe_load(text)
        if suffix == ".json":
            return json.loads(text)
    except Exception as exc:
        raise ServerRegistryError([f"Failed to parse {path}: {exc}"]) from exc
    raise ServerRegistryError([f"Unsupported config format {suffix!r} for {path} (use .yaml, .yml, or .json)"])


def _parse_specs(raw: Any) -> list[DownstreamServerSpec]:
    """Accept either a bare list of servers or a mapping with a 'servers' key."""
    if isinstance(raw, Mapping):
        raw = raw.get("servers")
    if raw is None:
        raise ServerRegistryError(["Config must contain a 'servers' list (or be a bare list of servers)"])
    if not isinstance(raw, list):
        raise ServerRegistryError([f"'servers' must be a list, got {type(raw).__name__}"])

    specs: list[DownstreamServerSpec] = []
    errors: list[str] = []
    for i, entry in enumerate(raw):
        spec, entry_errors = DownstreamServerSpec.from_dict(entry, position=i)
        errors.extend(entry_errors)
        if spec is not None:
            specs.append(spec)

    errors.extend(_validate_spec_set(specs))
    if errors:
        raise ServerRegistryError(errors)
    return specs


def _validate_spec_set(specs: list[DownstreamServerSpec]) -> list[str]:
    """Cross-spec validation: unique names and namespaces."""
    errors: list[str] = []
    seen_names: dict[str, int] = {}
    seen_namespaces: dict[str, str] = {}
    for spec in specs:
        errors.extend(spec.validate())
        if spec.name in seen_names:
            errors.append(f"duplicate server name {spec.name!r}")
        seen_names[spec.name] = 1
        if spec.namespace in seen_namespaces and seen_namespaces[spec.namespace] != spec.name:
            errors.append(f"namespace {spec.namespace!r} is claimed by both {seen_namespaces[spec.namespace]!r} and {spec.name!r}")
        seen_namespaces.setdefault(spec.namespace, spec.name)
    return errors


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
