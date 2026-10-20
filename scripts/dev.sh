#!/usr/bin/env bash
# Start the MCP server (8765), the simulator API (8766) and the web simulator (3000).
# Ctrl+C stops all three.
set -euo pipefail
cd "$(dirname "$0")/.."

export HDR_DB_PATH="${HDR_DB_PATH:-var/hdr.sqlite3}"
export HDR_APPROVAL_SECRET="${HDR_APPROVAL_SECRET:-$(uv run python -c 'import secrets; print(secrets.token_hex(32))')}"
export HDR_ADMIN_TOKEN="${HDR_ADMIN_TOKEN:-$(uv run python -c 'import secrets; print(secrets.token_hex(16))')}"
export HDR_MCP_URL="${HDR_MCP_URL:-http://127.0.0.1:8765/mcp}"
export NEXT_PUBLIC_HDR_API="${NEXT_PUBLIC_HDR_API:-http://127.0.0.1:8766}"

if [ ! -d apps/simulator/node_modules ]; then (cd apps/simulator && npm ci --no-audit --no-fund); fi

pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true' EXIT INT TERM
uv run python -m hdr.mcp_server & pids+=($!)
uv run python -m hdr.orchestrator & pids+=($!)
(cd apps/simulator && npm run dev) & pids+=($!)
echo "MCP server   http://127.0.0.1:8765/mcp"
echo "Simulator    http://127.0.0.1:3000"
wait
