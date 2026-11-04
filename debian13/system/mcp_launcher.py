#!/usr/bin/env python3
"""Launch the Debian 13 read-only stdio MCP server without embedding any secrets.

Installed Codex MCP config contains this path only. The operator token is read
locally at runtime from the existing private file (or inherited environment).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def get_token() -> str:
    value = os.environ.get("DEBIAN13_API_TOKEN", "")
    if not value:
        state_dir = Path(os.environ.get(
            "DEBIAN13_STATE_DIR",
            str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "debian13")
        ))
        token_path = state_dir / "operator-token"
        try:
            if token_path.stat().st_mode & 0o077:
                raise ValueError("Operator token file must be private (chmod 600).")
            value = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("Operator token file unavailable; start Debian 13 first.") from exc
    if len(value) < 32 or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        raise ValueError("Operator token invalid.")
    return value


def main() -> None:
    try:
        os.environ["DEBIAN13_API_TOKEN"] = get_token()
        port = int(os.environ.get("DEBIAN13_API_PORT", "8765"))
        if not 1 <= port <= 65535:
            raise ValueError("Invalid API port")
        os.environ["DEBIAN13_SYSTEM_API_URL"] = f"http://127.0.0.1:{port}"
    except ValueError as exc:
        print(f"MCP launcher cannot start: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    from mcp_server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
