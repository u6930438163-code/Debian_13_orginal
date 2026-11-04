---
name: debian13-management
description: Use when the user asks to check Debian13_orginal health, MCP connections, Ollama integration, original agent status, tasks, or Codex progress through the Debian 13 Connect local plugin.
---

# Debian13 Connect — read-only project management

- Discover MCP tools from the `debian13-local` server before describing what is connected.
- Verify that `initialize`, `tools/list`, and a harmless read-only call respond; report unavailable rather than guessing or providing simulated status.
- `debian13_profiles`: four configured profiles; they are **not** evidence of running workers.
- `debian13_original_agents`: separate original agent registry reachability from genuine inference or worker activity.
- `debian13_diagnostics`: real system/runtime status, not confirmation of an installed bootable OS.
- `debian13_integrations`: existing Codex, MCP and GPT Actions configuration, not subscription entitlements.
- `debian13_work_tasks`: persisted, operator-review tasks; a queued task is not running.
- `alex_existing_status` and `alex_existing_tasks`: external original Alex registry, never create or claim execution without proof.
- Never send or request root credentials, API keys, or hidden token files in chat; they are read locally by the bridge.
- Do not execute shell commands, perform purchases, make GitHub admin changes or auto-merge PRs through this plugin; its MCP tools are read-only.
- Distinguish public/private: this **local stdio** package is for a local desktop/runtime with Python and the Debian 13 server. ChatGPT web/mobile cannot access the user's localhost without a separately deployed authenticated HTTPS MCP endpoint.
- For progress reporting, calculate a percentage only against explicitly enumerated acceptance criteria and include tests, CI/PR links and remaining blockers.
