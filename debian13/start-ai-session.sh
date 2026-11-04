#!/usr/bin/env bash
# Run: bash debian13/start-ai-session.sh (repository root)
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE/system"
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker Engine is required for this AI session." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 is required." >&2
  exit 1
fi
if [[ -z "${DEBIAN13_API_TOKEN:-}" ]]; then
  umask 077
  TOKEN_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/debian13/operator-token"
  mkdir -p "$(dirname "$TOKEN_FILE")"
  if [[ ! -s "$TOKEN_FILE" ]]; then
    python3 -c 'import secrets;print(secrets.token_urlsafe(48))' > "$TOKEN_FILE"
  fi
  export DEBIAN13_API_TOKEN="$(cat "$TOKEN_FILE")"
fi
if [[ "${#DEBIAN13_API_TOKEN}" -lt 32 ]]; then
  echo "ERROR: operator token must have at least 32 characters." >&2
  exit 1
fi

if [[ -n "${MODEL_CHAT_URL:-}" ]]; then
  if [[ -z "${MODEL_API_KEY:-}" || -z "${MODEL_NAME:-}" ]]; then
    echo "ERROR: MODEL_CHAT_URL requires MODEL_API_KEY and MODEL_NAME." >&2
    exit 1
  fi
  echo "Starting Debian 13 with configured external model endpoint."
  echo "Text and images may be sent to the selected external model provider."
  docker compose -f compose.yaml up -d --build
else
  export OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2-vision:11b}"
  export OLLAMA_VISION_MODEL="${OLLAMA_VISION_MODEL:-llama3.2-vision:11b}"
  RAM_KIB="$(awk '/MemTotal:/ {print $2}' /proc/meminfo)"
  if [[ "$RAM_KIB" -lt 16777216 ]]; then
    echo "WARNING: less than 16 GiB RAM detected. Llama Vision 11B may fail or run slowly." >&2
    echo "Choose a larger Codespace/host or set MODEL_CHAT_URL, MODEL_API_KEY and MODEL_NAME." >&2
  fi
  echo "Starting Ollama; model $OLLAMA_MODEL is pulled automatically on first start."
  docker compose -f compose.yaml -f compose.ollama.yaml up -d --build
fi

export DEBIAN13_API_PORT="${DEBIAN13_PORT:-8080}"
export DEBIAN13_UI_PORT="${DEBIAN13_UI_PORT:-3000}"
echo "API: localhost:$DEBIAN13_API_PORT; GUI: localhost:$DEBIAN13_UI_PORT."
echo "Open the Codespaces Ports tab and choose the PRIVATE forwarded port 3000."
echo "Dialog Triady sessions and approved Ollama reports persist in SQLite."
echo "Ollama worker restarts with Docker; permanent 24/7 requires an always-on server."
echo "Codespaces may suspend after inactivity; keep Ports PRIVATE."
# Ollama mode includes a separate persistent UI container. External-provider mode
# retains the foreground Python UI until explicitly containerized as well.
if [[ -z "${MODEL_CHAT_URL:-}" ]]; then
  echo "Private GUI and AI worker now run as restartable Docker services."
  echo "Stop with: docker compose -f system/compose.yaml -f system/compose.ollama.yaml down"
  exit 0
fi
exec python3 "$BASE/ui/dev_server.py"
