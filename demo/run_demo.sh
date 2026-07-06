#!/usr/bin/env bash
# One-command, fully local demo of the enterprise MCP gateway.
#   ./demo/run_demo.sh
#
# Uses the repo's .venv if present; otherwise creates one and installs the
# package. No cloud, no network ports — downstreams are stdio subprocesses.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "[demo] creating venv at $VENV"
  "${PYTHON:-python3.12}" -m venv "$VENV"
fi

PY="$VENV/bin/python"

# Ensure the package + its runtime deps are importable.
if ! "$PY" -c "import sys; sys.path.insert(0,'src'); import mcp, mcp_org.proxy, yaml" >/dev/null 2>&1; then
  echo "[demo] installing dependencies into venv"
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -e .
fi

echo "[demo] python: $("$PY" --version)"
exec "$PY" "$REPO_ROOT/demo/run_demo.py"
