#!/usr/bin/env python3
"""Check local Debian 13 system-service readiness without probing a model."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def os_release_values(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ID", "VERSION_ID"}:
            values[key] = value.strip().strip('"\'')
    return values


def request_json(base_url: str, path: str, token: str | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base_url + path, headers=headers)
    try:
        response = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirectHandler()).open(request, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            raise ValueError("response too large")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("invalid JSON response")
        return response.status, data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Debian 13 local API core readiness")
    parser.add_argument(
        "--require-provider", action="store_true",
        help="also block when no optional model provider is configured (not inference proof)",
    )
    args = parser.parse_args(argv)
    base_url = os.environ.get("DEBIAN13_SYSTEM_API_URL", "http://127.0.0.1:8080").rstrip("/")
    try:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    except ValueError:
        print("BLOCKED: service URL must be a loopback HTTP origin.")
        return 1

    try:
        release = os_release_values()
    except (OSError, UnicodeError):
        release = {}
    debian_13 = release.get("ID") == "debian" and release.get("VERSION_ID") == "13"
    print(f"OS: {'PASS' if debian_13 else 'BLOCKED'} (Debian VERSION_ID=13 required)")

    try:
        status, health = request_json(base_url, "/healthz")
        healthy = status == 200 and health == {"status": "ok"}
    except (OSError, ValueError, json.JSONDecodeError):
        healthy = False
    print(f"Health: {'PASS' if healthy else 'BLOCKED'}")

    try:
        status, _ = request_json(base_url, "/v1/diagnostics")
        auth_enforced = status == 401
    except (OSError, ValueError, json.JSONDecodeError):
        auth_enforced = False
    print(f"Anonymous diagnostics rejected: {'PASS (401)' if auth_enforced else 'BLOCKED'}")

    token = os.environ.get("DEBIAN13_API_TOKEN", "")
    safe_token = token.isascii() and all(0x21 <= ord(char) <= 0x7E for char in token)
    if not safe_token or len(token) < 32:
        print("Authenticated diagnostics: BLOCKED (valid token not configured)")
        print("Model provider: NOT CHECKED; inference not tested")
        return 1

    try:
        diag_status, diagnostics = request_json(base_url, "/v1/diagnostics", token)
        profiles_status, profiles = request_json(base_url, "/v1/profiles", token)
        authenticated = (
            diag_status == 200
            and diagnostics.get("service") == "debian13-system"
            and profiles_status == 200
            and isinstance(profiles.get("providerConfigured"), bool)
        )
    except (OSError, ValueError, json.JSONDecodeError):
        authenticated = False
        diagnostics = {}
        profiles = {}
    print(f"Authenticated diagnostics: {'PASS' if authenticated else 'BLOCKED'}")

    provider_configured = profiles.get("providerConfigured") is True
    if provider_configured:
        print("Model provider: CONFIGURED; endpoint reachability and inference not tested")
    else:
        print("Model provider: NOT CONFIGURED; inference not tested")

    core_ready = debian_13 and healthy and auth_enforced and authenticated
    if args.require_provider and not provider_configured:
        print("Optional provider required by operator: BLOCKED")
    elif not provider_configured:
        print("Core API ready independently of optional AI configuration.")
    print("Inference: UNVERIFIED; no model request was made")
    return 0 if core_ready and (not args.require_provider or provider_configured) else 1


if __name__ == "__main__":
    sys.exit(main())
