#!/usr/bin/env python3
"""Optional local transport for ORIGINAL agent runtimes, regardless of AI engine.

This module only exchanges authenticated messages with the existing Debian 13
backend. It never calls a model, schedules replies or runs instructions.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.parse

AGENTS = frozenset({"aria", "alex", "astyria", "aurelius"})
RECIPIENTS = AGENTS | {"operator", "all"}
MAX_MESSAGE = 4000
MAX_RESPONSE_BYTES = 256 * 1024


class DialogClientError(Exception):
    """Safe, operator-readable error (never include headers or tokens)."""


def trusted_local_address(url: str) -> tuple[str, int]:
    """Never send bearer credentials to arbitrary URLs from the environment."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in
                ("127.0.0.1", "localhost", "::1") or parsed.username
                or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or not 1 <= parsed.port <= 65535):
            raise ValueError()
        return parsed.hostname, parsed.port
    except (ValueError, TypeError):
        raise DialogClientError("Dialog API must be a local HTTP loopback address with a port.") from None


def agent_token(agent: str) -> str:
    if agent not in AGENTS:
        raise DialogClientError("Unknown agent ID.")
    token = os.getenv("DEBIAN13_DIALOG_" + agent.upper() + "_TOKEN", "")
    if not 32 <= len(token) <= 512 or not all(33 <= ord(char) <= 126 for char in token):
        raise DialogClientError("An individual private agent token is required.")
    return token


def call(agent: str, method: str, *, to: str = "", content: str = "",
         after: int = 0, url: str | None = None) -> dict:
    """Read voluntary inbox or post a message. Never issues an LLM request."""
    host, port = trusted_local_address(
        url if url is not None else os.getenv("DEBIAN13_DIALOG_API_URL", "http://127.0.0.1:8765"))
    token = agent_token(agent)
    if method == "GET":
        if not isinstance(after, int) or not 0 <= after < 2**63:
            raise DialogClientError("Invalid message cursor.")
        path = "/api/v1/agent-dialog/messages?after=" + str(after)
        body = None
    elif method == "POST":
        if to not in RECIPIENTS or not isinstance(content, str) or not 1 <= len(content.strip()) <= MAX_MESSAGE:
            raise DialogClientError("Invalid recipient or message length.")
        if "\x00" in content:
            raise DialogClientError("NUL characters are not allowed.")
        path = "/api/v1/agent-dialog/messages"
        body = json.dumps({"to": to, "content": content.strip()},
                          ensure_ascii=False).encode("utf-8")
    else:
        raise DialogClientError("Unsupported method.")
    headers = {"Authorization": "Bearer " + token,
               "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        if response.status != (200 if method == "GET" else 201):
            raise DialogClientError("Dialog API returned an error or denied access.")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DialogClientError("Dialog API response exceeds limit.")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DialogClientError("Dialog API returned invalid JSON.") from None
        if not isinstance(data, dict):
            raise DialogClientError("Unexpected dialog response.")
        if method == "GET" and not isinstance(data.get("messages"), list):
            raise DialogClientError("Unexpected inbox response.")
        if method == "POST" and data.get("accepted") is not True:
            raise DialogClientError("Message was not accepted.")
        return data
    except (OSError, TimeoutError, http.client.HTTPException):
        raise DialogClientError("Local dialog API is unavailable.") from None
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local, optional AI-to-AI messages; no model calls.")
    parser.add_argument("--agent", required=True, choices=sorted(AGENTS))
    commands = parser.add_subparsers(dest="command", required=True)
    receive = commands.add_parser("inbox", help="Read available messages; never auto-replies.")
    receive.add_argument("--after", type=int, default=0)
    send = commands.add_parser("send", help="Read message body from stdin.")
    send.add_argument("--to", choices=sorted(RECIPIENTS), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inbox":
            result = call(args.agent, "GET", after=args.after)
        else:
            text = sys.stdin.read(MAX_MESSAGE + 2)
            if len(text) > MAX_MESSAGE + 1:
                raise DialogClientError("Message exceeds the 4000-character limit.")
            result = call(args.agent, "POST", to=args.to, content=text)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except DialogClientError as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
