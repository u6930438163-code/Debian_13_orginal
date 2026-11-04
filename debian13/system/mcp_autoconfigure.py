#!/usr/bin/env python3
"""MCP preflight and optional Codex registration without printing/storing tokens.

Usage:
  python3 debian13/system/mcp_autoconfigure.py --check
  python3 debian13/system/mcp_autoconfigure.py --install-codex

The Debian 13 API must already be running. Never expose this API directly on
the public Internet; any remote proxy must enforce authorization and TLS.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from mcp_launcher import get_token

MAX_REPLY = 262144


def _json_request(url: str, token: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/json, text/event-stream",
                 "Content-Type": "application/json",
                 "MCP-Protocol-Version": "2025-11-25"},
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=6) as response:
        data = response.read(MAX_REPLY + 1)
        if response.status != 200 or len(data) > MAX_REPLY:
            raise ValueError("MCP endpoint returned invalid status or a large reply.")
        parsed = json.loads(data)
    if not isinstance(parsed, dict) or "result" not in parsed:
        raise ValueError("MCP response is not JSON-RPC result.")
    return parsed["result"]


def check() -> dict:
    token = get_token()
    port = int(os.environ.get("DEBIAN13_API_PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid API port")
    url = f"http://127.0.0.1:{port}/mcp"
    initialized = _json_request(url, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "debian13-autoconfig", "version": "1"},
        },
    })
    listed = _json_request(url, token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    })
    names = [tool.get("name") for tool in listed.get("tools", []) if isinstance(tool, dict)]
    if "debian13_profiles" not in names:
        raise ValueError("Expected Debian 13 MCP tools were not returned.")
    return {"mcpHttpVerified": True, "protocol": initialized.get("protocolVersion"),
            "toolsAvailable": len(names), "stdioLauncherExists": (Path(__file__).parent / "mcp_launcher.py").is_file(),
            "codexCliDetected": bool(shutil.which("codex"))}


def install_codex() -> dict:
    status = check()
    exe = shutil.which("codex")
    if not exe:
        raise ValueError("Codex CLI not installed; install it, sign in, then retry.")
    launcher = str((Path(__file__).resolve().parent / "mcp_launcher.py"))
    # No token in arguments, environment output, or Codex's persistent config.
    result = subprocess.run([exe, "mcp", "add", "debian13-local", "--",
                             sys.executable, launcher],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=15, check=False)
    if result.returncode != 0:
        raise ValueError("Codex rejected the MCP registration; inspect Codex CLI locally.")
    status["codexMcpRegistered"] = True
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure private Debian 13 MCP")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Test the local HTTP MCP endpoint")
    group.add_argument("--install-codex", action="store_true", help="Register a private stdio MCP bridge in local Codex")
    args = parser.parse_args()
    try:
        result = install_codex() if args.install_codex else check()
    except (ValueError, OSError, urllib.error.URLError, json.JSONDecodeError,
            subprocess.TimeoutExpired) as exc:
        # Never print exception internals, URLs, tokens or provider response bodies.
        print(json.dumps({"ok": False, "reason": type(exc).__name__,
                          "hint": "Check local API status, private token file and Codex installation."}))
        raise SystemExit(1) from None
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
