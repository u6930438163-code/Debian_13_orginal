#!/usr/bin/env python3
"""Private local stdio MCP client bridge to a running Debian13_orginal API.

This bridge does not execute commands, accept public remote URLs, store secrets,
start Ollama, or claim external workers are connected.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, ProxyHandler, build_opener

MAX_INPUT = 65536
MAX_OUTPUT = 262144


class NoRedirect(HTTPRedirectHandler):
    """Never allow localhost MCP to redirect a bearer token elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def operator_token() -> str:
    value = os.environ.get("DEBIAN13_API_TOKEN", "")
    if not value:
        state = Path(os.environ.get("DEBIAN13_STATE_DIR",
                                    str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "debian13")))
        token_file = state / "operator-token"
        if token_file.stat().st_mode & 0o077:
            raise ValueError("The operator token must be private (chmod 600).")
        value = token_file.read_text(encoding="utf-8").strip()
    if len(value) < 32 or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        raise ValueError("Invalid operator token")
    return value


def forward(request: dict) -> dict | None:
    port = int(os.environ.get("DEBIAN13_API_PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid local API port")
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_INPUT:
        raise ValueError("Request too large")
    upstream = Request(
        "http://127.0.0.1:%d/mcp" % port,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + operator_token(),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
    )
    with build_opener(ProxyHandler({}), NoRedirect()).open(upstream, timeout=12) as reply:
        if reply.status == 202:
            return None
        if reply.status != 200:
            raise ValueError("MCP response status invalid")
        data = reply.read(MAX_OUTPUT + 1)
        if len(data) > MAX_OUTPUT:
            raise ValueError("MCP response exceeds limit")
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError("Invalid MCP JSON-RPC response")
        return parsed


def main() -> None:
    for line in sys.stdin.buffer:
        request = None
        if len(line) > MAX_INPUT:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError("Invalid JSON-RPC request")
            result = forward(request)
        except (OSError, ValueError, HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            result = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                      "error": {"code": -32000, "message": "Debian13 local API is offline or unavailable."}}
        if result is not None:
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
