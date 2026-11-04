#!/usr/bin/env bash
# Run the Debian 13 application (API + GUI + persistent Work) with Python only.
# No Docker, npm, paid GitHub subscription, Copilot or large model required.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
usage() {
  cat <<'HELP'
Debian 13 development console — one command
  bash debian13/start-local.sh [--check | --help]

Default: serve the real Python API + interactive UI on PRIVATE loopback ports.
Connect an existing AI provider with MODEL_CHAT_URL, MODEL_API_KEY, MODEL_NAME;
without it the UI works, while AI responses correctly report unavailable.
Default ports: 3000 for GUI, 8765 for backend (both localhost only).
Free Ollama text chat (when installed and running on this host):
  DEBIAN13_LOCAL_OLLAMA=1 OLLAMA_MODEL=llama3.2:3b bash debian13/start-local.sh
This mode never uses OpenAI API or a paid cloud fallback.
HELP
}
case "${1:-}" in
  --help|-h) usage; exit 0 ;;
  --check) ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: Python 3 (3.10+) is required." >&2
  exit 1
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("ERROR: Python 3.10 or later required.")
PY

API_PORT="${DEBIAN13_API_PORT:-8765}"
UI_PORT="${DEBIAN13_UI_PORT:-3000}"
python3 - "$API_PORT" "$UI_PORT" <<'PY'
import sys
try:
    api, ui = (int(value) for value in sys.argv[1:])
    assert 1 <= api <= 65535 and 1 <= ui <= 65535 and api != ui
except (ValueError, AssertionError):
    raise SystemExit("ERROR: Provide distinct valid ports DEBIAN13_API_PORT and DEBIAN13_UI_PORT.")
PY
if [[ "${DEBIAN13_LOCAL_OLLAMA:-0}" != "1" && -n "${MODEL_CHAT_URL:-}" ]]; then
  if [[ -z "${MODEL_API_KEY:-}" || -z "${MODEL_NAME:-}" ]]; then
    echo "ERROR: MODEL_CHAT_URL also requires MODEL_API_KEY and MODEL_NAME." >&2
    exit 1
  fi
fi
if [[ "${1:-}" == "--check" ]]; then
  python3 - "$ROOT" <<'PY'
from pathlib import Path
import ast, sys
root = Path(sys.argv[1])
for path in [root / "system/server.py", root / "system/work_store.py",
             root / "system/alex_bridge.py", root / "system/alex_handoff.py",
             root / "system/chat_memory.py", root / "system/codex_status.py",
             root / "system/ai_center_worker.py", root / "system/mcp_autoconfigure.py",
             root / "system/mcp_launcher.py",
             root / "system/ui_sync.py", root / "system/github_dashboard.py",
             root / "system/original_agent_handoff.py",
             root / "ui/dev_server.py"]:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("PASS: Python runtime, launch ports and application modules are valid.")
PY
  exit 0
fi

# Explicit, free, local provider mode. Never use a paid fallback, even when
# unrelated cloud keys are present on the host. No secrets stored in Git.
if [[ "${DEBIAN13_LOCAL_OLLAMA:-0}" != "0" && "${DEBIAN13_LOCAL_OLLAMA:-0}" != "1" ]]; then
  echo "ERROR: DEBIAN13_LOCAL_OLLAMA must be 0 or 1." >&2
  exit 1
fi
if [[ "${DEBIAN13_LOCAL_OLLAMA:-0}" == "1" ]]; then
  OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2:3b}"
  if [[ ! "$OLLAMA_MODEL" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,119}$ ]]; then
    echo "ERROR: Invalid local OLLAMA_MODEL name." >&2
    exit 1
  fi
  # Force loopback; ignore MODEL_CHAT_URL and any remote keys/fallbacks.
  export MODEL_CHAT_URL="http://127.0.0.1:11434/v1/chat/completions"
  export MODEL_API_KEY="ollama-local-no-paid-api"
  export MODEL_NAME="$OLLAMA_MODEL"
  export OLLAMA_VISION_URL="http://127.0.0.1:11434/v1/chat/completions"
  export VISION_FALLBACK_ENABLED=0
  unset VISION_FALLBACK_CHAT_URL VISION_FALLBACK_API_KEY VISION_FALLBACK_MODEL
fi

STATE_DIR="${DEBIAN13_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/debian13}"
WORKSPACE_DIR="${WORKSPACE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/debian13/workspace}"
mkdir -p "$STATE_DIR" "$WORKSPACE_DIR"
chmod 700 "$STATE_DIR" "$WORKSPACE_DIR"
TOKEN_FILE="$STATE_DIR/operator-token"
if [[ -z "${DEBIAN13_API_TOKEN:-}" ]]; then
  if [[ ! -s "$TOKEN_FILE" ]]; then
    python3 -c 'import secrets;print(secrets.token_urlsafe(48))' > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
  fi
  export DEBIAN13_API_TOKEN
  DEBIAN13_API_TOKEN="$(cat "$TOKEN_FILE")"
fi
if (( ${#DEBIAN13_API_TOKEN} < 32 )); then
  echo "ERROR: DEBIAN13_API_TOKEN must be at least 32 characters." >&2
  exit 1
fi

export WORKSPACE_DIR HOST=127.0.0.1 PORT="$API_PORT"
export DEBIAN13_API_PORT="$API_PORT" DEBIAN13_UI_PORT="$UI_PORT"
python3 -u "$ROOT/system/server.py" >"$STATE_DIR/api.log" 2>&1 &
API_PID=$!
UI_PID=""
cleanup() {
  trap - EXIT INT TERM HUP
  for child in "$UI_PID" "$API_PID"; do
    if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then
      kill "$child" 2>/dev/null || true
    fi
  done
  for child in "$UI_PID" "$API_PID"; do
    if [[ -n "$child" ]]; then
      wait "$child" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

for _attempt in $(seq 1 40); do
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "ERROR: Backend stopped. See $STATE_DIR/api.log" >&2
    exit 1
  fi
  if python3 - "$API_PORT" <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:" + sys.argv[1] + "/healthz", timeout=.5) as r:
        assert r.status == 200
except Exception:
    sys.exit(1)
PY
  then
    break
  fi
  sleep .15
done
if ! python3 - "$API_PORT" <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:" + sys.argv[1] + "/healthz", timeout=1) as r:
        assert r.status == 200
except Exception:
    sys.exit(1)
PY
then
  echo "ERROR: Backend did not become ready. See $STATE_DIR/api.log" >&2
  exit 1
fi

echo "Debian 13 console is starting:"
echo "  GUI: http://127.0.0.1:$UI_PORT"
echo "  API: http://127.0.0.1:$API_PORT (authenticated, loopback only)"
echo "  Work: persistent SQLite in private workspace"
echo "  Alex: not started; original agent requires independent connected runtime"
if [[ "${DEBIAN13_LOCAL_OLLAMA:-0}" == "1" ]]; then
  echo "  AI: local Ollama $MODEL_NAME configured on 127.0.0.1:11434; no paid API fallback"
  echo "  AI: verify Ollama availability and installed model separately; configuration is not inference"
fi
if [[ -n "${MODEL_CHAT_URL:-}" ]]; then
  echo "  AI: provider configured; live inference must still be checked"
else
  echo "  AI: not configured; connect Ollama or another compatible provider for chat"
fi
echo "  Stop with Ctrl+C. In Codespaces use the PRIVATE forwarded port $UI_PORT."
python3 -u "$ROOT/ui/dev_server.py" &
UI_PID=$!
wait "$UI_PID"
