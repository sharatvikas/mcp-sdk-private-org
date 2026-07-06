"""Tests for the downstream server registry — loading, validation, hot reload."""

from __future__ import annotations

import json
import os
import time

import pytest

from mcp_org.registry.servers import (
    DownstreamServerSpec,
    ServerRegistry,
    ServerRegistryError,
)


def _spec(**overrides) -> DownstreamServerSpec:
    base = dict(name="runbooks", namespace="runbooks", transport="stdio", command="python")
    base.update(overrides)
    return DownstreamServerSpec(**base)


# ─── Spec validation ─────────────────────────────────────────────────────────

class TestSpecValidation:
    def test_valid_stdio_spec(self):
        assert _spec().validate() == []

    def test_valid_sse_spec(self):
        spec = _spec(transport="sse", command=None, url="http://mcp.internal:8080/sse")
        assert spec.validate() == []

    def test_valid_streamable_http_spec(self):
        spec = _spec(transport="streamable-http", command=None, url="https://mcp.internal/mcp")
        assert spec.validate() == []

    def test_stdio_requires_command(self):
        errors = _spec(command=None).validate()
        assert any("requires 'command'" in e for e in errors)

    def test_sse_requires_url(self):
        errors = _spec(transport="sse", command=None).validate()
        assert any("requires 'url'" in e for e in errors)

    def test_url_must_be_http(self):
        errors = _spec(transport="sse", command=None, url="ftp://nope").validate()
        assert any("must start with http" in e for e in errors)

    def test_stdio_rejects_url(self):
        errors = _spec(url="http://also-a-url").validate()
        assert any("'url' is not valid for stdio" in e for e in errors)

    def test_sse_rejects_command(self):
        errors = _spec(transport="sse", url="http://x/sse").validate()
        assert any("'command' is not valid" in e for e in errors)

    def test_invalid_transport(self):
        errors = _spec(transport="websocket").validate()
        assert any("invalid transport" in e for e in errors)

    def test_invalid_namespace_characters(self):
        errors = _spec(namespace="bad namespace!").validate()
        assert any("invalid namespace" in e for e in errors)

    def test_invalid_name(self):
        errors = _spec(name="-leading-dash").validate()
        assert any("invalid name" in e for e in errors)

    def test_nonpositive_timeout_rejected(self):
        errors = _spec(request_timeout_seconds=0).validate()
        assert any("request_timeout_seconds" in e for e in errors)

    def test_allows_teams_wildcard(self):
        assert _spec(allowed_teams=("*",)).allows_teams(["anything"]) is True

    def test_allows_teams_intersection(self):
        spec = _spec(allowed_teams=("sre-team", "security"))
        assert spec.allows_teams(["dev-team", "sre-team"]) is True
        assert spec.allows_teams(["dev-team"]) is False

    def test_from_dict_rejects_unknown_keys(self):
        spec, errors = DownstreamServerSpec.from_dict(
            {"name": "x", "namespace": "x", "command": "y", "comand_typo": "z"}
        )
        assert spec is None
        assert any("unknown field" in e for e in errors)

    def test_from_dict_requires_name_and_namespace(self):
        spec, errors = DownstreamServerSpec.from_dict({"transport": "stdio", "command": "x"})
        assert spec is None
        assert any("'name' is required" in e for e in errors)
        assert any("'namespace' is required" in e for e in errors)

    def test_from_dict_normalises_args_to_tuple(self):
        spec, errors = DownstreamServerSpec.from_dict(
            {"name": "a", "namespace": "a", "command": "python", "args": ["-m", "srv"]}
        )
        assert errors == []
        assert spec is not None and spec.args == ("-m", "srv")

    def test_from_dict_rejects_empty_allowed_teams(self):
        spec, errors = DownstreamServerSpec.from_dict(
            {"name": "a", "namespace": "a", "command": "x", "allowed_teams": []}
        )
        assert spec is None
        assert any("allowed_teams" in e for e in errors)


# ─── Registry loading ────────────────────────────────────────────────────────

class TestRegistryLoading:
    def test_load_yaml_file(self, tmp_path):
        config = tmp_path / "servers.yaml"
        config.write_text(
            """
servers:
  - name: runbooks
    namespace: runbooks
    transport: stdio
    command: python
    args: ["-m", "runbooks_mcp"]
  - name: incidents
    namespace: incidents
    transport: sse
    url: http://incidents.internal/sse
    allowed_teams: [sre-team]
"""
        )
        registry = ServerRegistry.from_file(config)
        assert len(registry) == 2
        assert registry.get("runbooks").command == "python"
        assert registry.get_by_namespace("incidents").url == "http://incidents.internal/sse"

    def test_load_json_file(self, tmp_path):
        config = tmp_path / "servers.json"
        config.write_text(
            json.dumps(
                {"servers": [{"name": "a", "namespace": "a", "transport": "stdio", "command": "x"}]}
            )
        )
        registry = ServerRegistry.from_file(config)
        assert len(registry) == 1

    def test_load_bare_list(self, tmp_path):
        config = tmp_path / "servers.yaml"
        config.write_text("- name: a\n  namespace: a\n  command: x\n")
        assert len(ServerRegistry.from_file(config)) == 1

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ServerRegistryError, match="not found"):
            ServerRegistry.from_file(tmp_path / "nope.yaml")

    def test_unsupported_extension_raises(self, tmp_path):
        config = tmp_path / "servers.toml"
        config.write_text("x = 1")
        with pytest.raises(ServerRegistryError, match="Unsupported config format"):
            ServerRegistry.from_file(config)

    def test_malformed_yaml_raises(self, tmp_path):
        config = tmp_path / "servers.yaml"
        config.write_text("servers: [unclosed")
        with pytest.raises(ServerRegistryError, match="Failed to parse"):
            ServerRegistry.from_file(config)

    def test_missing_servers_key_raises(self, tmp_path):
        config = tmp_path / "servers.yaml"
        config.write_text("other_key: true")
        with pytest.raises(ServerRegistryError, match="'servers' list"):
            ServerRegistry.from_file(config)

    def test_duplicate_names_rejected(self):
        with pytest.raises(ServerRegistryError, match="duplicate server name"):
            ServerRegistry.from_dict(
                [
                    {"name": "a", "namespace": "a", "command": "x"},
                    {"name": "a", "namespace": "b", "command": "x"},
                ]
            )

    def test_duplicate_namespaces_rejected(self):
        with pytest.raises(ServerRegistryError, match="namespace 'shared' is claimed"):
            ServerRegistry.from_dict(
                [
                    {"name": "a", "namespace": "shared", "command": "x"},
                    {"name": "b", "namespace": "shared", "command": "x"},
                ]
            )

    def test_all_errors_reported_at_once(self, tmp_path):
        config = tmp_path / "servers.yaml"
        config.write_text(
            """
servers:
  - name: a
    namespace: a
    transport: stdio
  - name: b
    namespace: b
    transport: sse
"""
        )
        with pytest.raises(ServerRegistryError) as exc_info:
            ServerRegistry.from_file(config)
        assert len(exc_info.value.errors) >= 2  # missing command AND missing url


# ─── Programmatic API ────────────────────────────────────────────────────────

class TestProgrammaticAPI:
    def test_register_and_get(self):
        registry = ServerRegistry()
        registry.register(_spec())
        assert registry.get("runbooks") is not None
        assert registry.namespaces() == ["runbooks"]

    def test_register_invalid_spec_raises(self):
        registry = ServerRegistry()
        with pytest.raises(ServerRegistryError):
            registry.register(_spec(command=None))

    def test_register_conflicting_namespace_raises(self):
        registry = ServerRegistry([_spec()])
        with pytest.raises(ServerRegistryError, match="claimed"):
            registry.register(_spec(name="other", namespace="runbooks"))

    def test_deregister(self):
        registry = ServerRegistry([_spec()])
        removed = registry.deregister("runbooks")
        assert removed.name == "runbooks"
        assert len(registry) == 0

    def test_deregister_missing_raises_keyerror(self):
        with pytest.raises(KeyError):
            ServerRegistry().deregister("ghost")

    def test_enabled_only_filter(self):
        registry = ServerRegistry([_spec(), _spec(name="dark", namespace="dark", enabled=False)])
        assert len(registry.list_servers()) == 2
        assert [s.name for s in registry.list_servers(enabled_only=True)] == ["runbooks"]

    def test_register_replaces_existing(self):
        registry = ServerRegistry([_spec()])
        registry.register(_spec(command="python3"))
        assert registry.get("runbooks").command == "python3"
        assert len(registry) == 1


# ─── Tool name resolution ────────────────────────────────────────────────────

class TestResolveTool:
    def test_resolves_namespaced_tool(self):
        registry = ServerRegistry([_spec()])
        spec, local = registry.resolve_tool("runbooks__search_runbooks")
        assert spec.name == "runbooks"
        assert local == "search_runbooks"

    def test_local_name_may_contain_separator(self):
        registry = ServerRegistry([_spec()])
        _, local = registry.resolve_tool("runbooks__weird__tool")
        assert local == "weird__tool"

    def test_unknown_namespace_raises(self):
        registry = ServerRegistry([_spec()])
        with pytest.raises(KeyError, match="No downstream server owns namespace 'ghost'"):
            registry.resolve_tool("ghost__tool")

    def test_missing_separator_raises(self):
        registry = ServerRegistry([_spec()])
        with pytest.raises(KeyError, match="no namespace prefix"):
            registry.resolve_tool("bare_tool_name")

    def test_custom_separator(self):
        registry = ServerRegistry([_spec()])
        spec, local = registry.resolve_tool("runbooks.search", separator=".")
        assert spec.name == "runbooks" and local == "search"


# ─── Hot reload ──────────────────────────────────────────────────────────────

def _write_config(path, names: list[str]) -> None:
    servers = [{"name": n, "namespace": n, "transport": "stdio", "command": "python"} for n in names]
    path.write_text(json.dumps({"servers": servers}))


class TestHotReload:
    def test_maybe_reload_noop_when_unchanged(self, tmp_path):
        config = tmp_path / "servers.json"
        _write_config(config, ["a"])
        registry = ServerRegistry.from_file(config)
        assert registry.maybe_reload() is False

    def test_maybe_reload_picks_up_new_server(self, tmp_path):
        config = tmp_path / "servers.json"
        _write_config(config, ["a"])
        registry = ServerRegistry.from_file(config)

        _write_config(config, ["a", "b"])
        os.utime(config, (time.time() + 5, time.time() + 5))  # force mtime change

        assert registry.maybe_reload() is True
        assert registry.get("b") is not None

    def test_maybe_reload_ignores_touch_without_content_change(self, tmp_path):
        config = tmp_path / "servers.json"
        _write_config(config, ["a"])
        registry = ServerRegistry.from_file(config)
        os.utime(config, (time.time() + 5, time.time() + 5))
        assert registry.maybe_reload() is False

    def test_bad_reload_keeps_previous_config(self, tmp_path):
        config = tmp_path / "servers.json"
        _write_config(config, ["a"])
        registry = ServerRegistry.from_file(config)

        config.write_text("{not json")
        os.utime(config, (time.time() + 5, time.time() + 5))

        assert registry.maybe_reload() is False  # swallowed, logged
        assert registry.get("a") is not None  # old config still active

    def test_reload_reports_added_removed_changed(self, tmp_path):
        config = tmp_path / "servers.json"
        _write_config(config, ["a", "b"])
        registry = ServerRegistry.from_file(config)

        servers = [
            {"name": "a", "namespace": "a", "transport": "stdio", "command": "python3"},  # changed
            {"name": "c", "namespace": "c", "transport": "stdio", "command": "python"},  # added
        ]
        config.write_text(json.dumps({"servers": servers}))

        added, removed, changed = registry.reload()
        assert added == {"c"}
        assert removed == {"b"}
        assert changed == {"a"}

    def test_subscriber_notified_on_reload(self, tmp_path):
        config = tmp_path / "servers.json"
        _write_config(config, ["a"])
        registry = ServerRegistry.from_file(config)

        events = []
        registry.subscribe(lambda add, rem, chg: events.append((add, rem, chg)))
        _write_config(config, ["a", "b"])
        registry.reload()

        assert events == [({"b"}, set(), set())]

    def test_reload_without_source_raises(self):
        with pytest.raises(RuntimeError, match="not loaded from a file"):
            ServerRegistry([_spec()]).reload()

    def test_failing_subscriber_does_not_break_registry(self, tmp_path):
        config = tmp_path / "servers.json"
        _write_config(config, ["a"])
        registry = ServerRegistry.from_file(config)

        def bad_callback(add, rem, chg):
            raise RuntimeError("subscriber bug")

        registry.subscribe(bad_callback)
        _write_config(config, ["a", "b"])
        registry.reload()  # must not raise
        assert registry.get("b") is not None
