#!/usr/bin/env bash
# Equivalent Web entry to: uv run agentcore web --config agentcore.toml
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$SCRIPT_DIR"

UV_COMMAND=uv
CONF_DIR="$SCRIPT_DIR"

# PowerShell's bash.exe can launch WSL. Keep the Windows runtime used by
# the configured RAG interpreter and translate the config path for uv.exe.
if [[ -n "${WSL_DISTRO_NAME:-}" || -n "${WSL_INTEROP:-}" ]]; then
    UV_COMMAND=uv.exe
    CONF_DIR="$(wslpath -w "$SCRIPT_DIR")"
fi

if ! command -v "$UV_COMMAND" >/dev/null 2>&1; then
    printf '%s\n' "$UV_COMMAND is required. Install uv on the host and add it to PATH." >&2
    exit 127
fi

exec "$UV_COMMAND" run --locked \
    --extra openai \
    --extra mcp \
    --extra supabase \
    --extra embedding \
    --extra cognee \
    funagent --conf-dir "$CONF_DIR" "$@"
