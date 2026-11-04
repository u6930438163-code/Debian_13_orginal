# Debian13 Connect (0.1.0, local preview)

A private local plugin for ChatGPT/Codex desktop configurations which support stdio MCP.

1. Start the Debian 13 API privately on your local host with `bash debian13/start-local.sh`.
2. Ensure the generated operator token exists under `~/.local/state/debian13/operator-token` (mode 0600) or supply `DEBIAN13_API_TOKEN` through a trusted secrets store.
3. Install/import this plugin using a **local MCP-capable client**. It bridges to `http://127.0.0.1:8765/mcp` and never contains the operator token.
4. Test `initialize`, `tools/list`, `debian13_diagnostics` and `debian13_original_agents`.

This plugin does not start Ollama, workers, Codex or the Debian 13 API. It cannot control root.
**Android/web ChatGPT requires a separate, reachable, authenticated HTTPS MCP service.** Do not publish port 8765 or add a made-up remote address to `mcp.json`.
