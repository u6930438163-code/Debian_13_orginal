#!/usr/bin/env python3
"""Small authenticated API for the standalone Debian 13 system prototype."""

from __future__ import annotations

import base64
import binascii
import hmac
import http.client
import http.server
import json
import os
import platform
import re
import selectors
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from work_store import WorkStore
from github_dashboard import DashboardCache, GitHubUnavailable
from ui_sync import SyncStore, SyncValidationError, parse_title_command
from chat_memory import ChatMemory
import mcp_server
from codex_status import report as codex_status_report
from integration_progress import report as integration_progress_report
from svl_compat import inspect_svl, terminal_svl_report
from original_agents import report as original_agents_report
from alex_handoff import AlexHandoffError, capabilities as alex_capabilities, submit_task as submit_to_alex
from original_agent_handoff import (
    OriginalAgentError, capabilities as original_review_capabilities,
    submit as submit_original_review, status as original_review_status,
)

MAX_BODY_BYTES = 16 * 1024
MAX_VISION_BODY_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_MESSAGE_CHARS = 4000
MAX_COMMAND_CHARS = 2000
COMMAND_TIMEOUT_SECONDS = 10
CHAT_TIMEOUT_SECONDS = 110
RATE_WINDOW_SECONDS = 60
RATE_LIMIT = 30

TASK_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
TASK_DETAIL_PATH = re.compile(r"^/api/v1/tasks/(" + TASK_UUID + r")$")
TASK_CANCEL_PATH = re.compile(r"^/api/v1/tasks/(" + TASK_UUID + r")/cancel$")
SESSION_MESSAGES_PATH = re.compile(r"^/api/v1/sessions/(" + TASK_UUID + r")/messages$")
TASK_DISPATCH_PATH = re.compile(r"^/api/v1/tasks/(" + TASK_UUID + r")/dispatch$")
TASK_ORIGINAL_HANDOFF_PATH = re.compile(r"^/api/v1/tasks/(" + TASK_UUID + r")/handoff$")
TASK_ORIGINAL_STATUS_PATH = re.compile(r"^/api/v1/tasks/(" + TASK_UUID + r")/original-status$")
TASK_APPROVE_ANALYSIS_PATH = re.compile(r"^/api/v1/tasks/(" + TASK_UUID + r")/approve-analysis$")
WORK_KINDS = frozenset({"development", "bugfix", "audit", "research"})

PROFILES = {
    "aria": {"name": "Aria"},
    "astyria": {"name": "Astyria"},
    "aurelius": {"name": "Aurelius"},
    "alex": {"name": "Alex"},
}

COMMANDS = {
    "pwd": "/usr/bin/pwd",
    "ls": "/usr/bin/ls",
    "cat": "/usr/bin/cat",
    "head": "/usr/bin/head",
    "tail": "/usr/bin/tail",
    "wc": "/usr/bin/wc",
    "grep": "/usr/bin/grep",
    "date": "/usr/bin/date",
    "whoami": "/usr/bin/whoami",
    "id": "/usr/bin/id",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_provider_url(value: str) -> str:
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme == "https":
        allowed = True
    elif parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        allowed = True
    elif parsed.scheme == "http" and parsed.hostname == "ollama" and os.environ.get("DEBIAN13_ALLOW_INTERNAL_OLLAMA") == "1" and parsed.port == 11434:
        allowed = True
    else:
        allowed = False
    if (
        not allowed
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/v1/chat/completions")
    ):
        raise ValueError("MODEL_CHAT_URL must be an HTTPS chat-completions URL (HTTP is allowed for loopback tests).")
    return value


def safe_workspace_path(workspace: Path, raw_path: str) -> str:
    if not raw_path or raw_path.startswith("-"):
        raise ApiError(400, "invalid_path", "File arguments must be relative workspace paths.")
    candidate = (workspace / raw_path).resolve()
    try:
        relative = candidate.relative_to(workspace)
    except ValueError as exc:
        raise ApiError(403, "path_outside_workspace", "Path is outside the terminal workspace.") from exc
    return str(relative) if str(relative) != "." else "."


def command_argv(command: str, workspace: Path) -> list[str]:
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
        raise ApiError(400, "invalid_command", "Supply a command of 1–2000 characters.")
    try:
        parts = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ApiError(400, "invalid_command", "Command quoting is invalid.") from exc
    if not parts or parts[0] not in COMMANDS:
        raise ApiError(403, "command_not_allowed", "This terminal command is not in the allowed command list.")

    name, args = parts[0], parts[1:]
    if name in {"pwd", "date", "whoami", "id"}:
        if args:
            raise ApiError(400, "invalid_arguments", f"{name} does not accept arguments.")
        return [COMMANDS[name]]

    if name == "grep":
        if len(args) < 2 or args[0].startswith("-") or len(args[0]) > 200:
            raise ApiError(400, "invalid_arguments", "Use grep PATTERN WORKSPACE_FILE [...].")
        files = [safe_workspace_path(workspace, path) for path in args[1:]]
        return [COMMANDS[name], "--", args[0], *files]

    if name in {"head", "tail"}:
        count = "10"
        if args and args[0] == "-n":
            if (
                len(args) < 3
                or not args[1].isdigit()
                or len(args[1]) > 3
                or not 1 <= int(args[1]) <= 200
            ):
                raise ApiError(400, "invalid_arguments", f"Use {name} [-n 1..200] WORKSPACE_FILE [...].")
            count, args = args[1], args[2:]
        if not args:
            raise ApiError(400, "invalid_arguments", f"{name} requires a workspace file.")
        files = [safe_workspace_path(workspace, path) for path in args]
        return [COMMANDS[name], "-n", count, "--", *files]

    if name == "ls":
        paths = [safe_workspace_path(workspace, path) for path in args] if args else ["."]
        return [COMMANDS[name], "--", *paths]

    if name in {"cat", "wc"}:
        paths = [safe_workspace_path(workspace, path) for path in args] if args else []
        return [COMMANDS[name], "--", *paths]

    raise ApiError(403, "command_not_allowed", "This terminal command is not in the allowed command list.")


def run_terminal(command: str, workspace: Path) -> dict:
    if isinstance(command, str) and command.strip():
        try:
            svl_parts = shlex.split(command, posix=True)
        except ValueError:
            raise ApiError(400, "invalid_command", "Command quoting is invalid.") from None
        if svl_parts and svl_parts[0] == "svl":
            if len(svl_parts) != 2 or svl_parts[1] not in {"status", "report", "profiles"}:
                raise ApiError(400, "invalid_svl_command",
                               "Use: svl status | svl report | svl profiles.")
            return terminal_svl_report(workspace, svl_parts[1])
    argv = command_argv(command, workspace)
    try:
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            env={"HOME": str(workspace), "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise ApiError(500, "command_start_failed", "The terminal command could not be started.") from exc

    selector = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    for stream, key in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        selector.register(stream, selectors.EVENT_READ, key)
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    total = 0
    try:
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise ApiError(504, "command_timeout", "The terminal command exceeded its time limit.")
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), min(65536, MAX_OUTPUT_BYTES + 1 - total))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > MAX_OUTPUT_BYTES:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise ApiError(413, "output_too_large", "Terminal output exceeded the 1 MiB limit.")
                output[key.data].extend(chunk)
        code = process.wait()
    finally:
        selector.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()
    return {
        "exitCode": code,
        "stdout": output["stdout"].decode("utf-8", errors="replace"),
        "stderr": output["stderr"].decode("utf-8", errors="replace"),
    }


def validate_image(image: object) -> str:
    """Validate PNG/JPEG base64 and return a bounded data URI for the model."""
    if not isinstance(image, dict) or set(image) != {"mime_type", "base64"}:
        raise ApiError(400, "invalid_image", "Expected an image with mime_type and base64.")
    mime = image.get("mime_type")
    encoded = image.get("base64")
    if mime not in ("image/png", "image/jpeg") or not isinstance(encoded, str):
        raise ApiError(400, "invalid_image", "Only PNG and JPEG images are supported.")
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ApiError(413, "image_too_large", "Image exceeds 2 MiB.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(400, "invalid_image", "Invalid base64-encoded image.") from exc
    if len(raw) > MAX_IMAGE_BYTES or not raw:
        raise ApiError(413, "image_too_large", "Image exceeds 2 MiB or is empty.")
    if mime == "image/png":
        correct = raw.startswith(b"\x89PNG\r\n\x1a\n")
    else:
        correct = raw.startswith(b"\xff\xd8\xff")
    if not correct:
        raise ApiError(400, "invalid_image", "Image bytes do not match the declared MIME type.")
    return "data:" + mime + ";base64," + encoded


def is_ollama_endpoint(url: str) -> bool:
    """Recognise Docker Ollama and local Ollama on its native port.

    The /v1/chat/completions image payload for Ollama uses an image_url string.
    Do not assume arbitrary HTTPS OpenAI-compatible providers are Ollama.
    """
    parsed = urllib.parse.urlsplit(url)
    return (parsed.hostname == "ollama" and parsed.port == 11434) or (
        parsed.hostname in ("127.0.0.1", "localhost", "::1") and parsed.port == 11434
    )


def complete_chat(profile: str, message: str, config: dict, image: str | None = None,
                  history: list[dict] | None = None,
                  notes: list[dict] | None = None,
                  ollama_image_format: bool = False) -> tuple[str, str]:
    if not config["model_chat_url"] or not config["model_api_key"] or not config["model_name"]:
        raise ApiError(503, "provider_not_configured", "No AI model provider is configured.")
    model = config.get("vision_model_name", "") if image is not None else config["model_name"]
    if not model:
        raise ApiError(503, "vision_not_configured", "A vision model is not configured.")
    is_ollama = ollama_image_format or is_ollama_endpoint(config["model_chat_url"])
    user_content = message if image is None else [
        {"type": "text", "text": message},
        # Ollama's /v1 Chat Completions docs use a string image_url.
        {"type": "image_url", "image_url": image if is_ollama else {"url": image}},
    ]
    # No default persona, role, system prompt, custom instructions, or saved
    # notes are injected into the model. The operator assigns tasks explicitly.
    # Provider-specific defaults remain outside this application's control.
    messages = []
    for item in (history or [])[-24:]:
        if item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str):
            messages.append({"role": item["role"], "content": item["content"][:4000]})
    messages.append({"role": "user", "content": user_content})
    payload = json.dumps({
        "model": model, "messages": messages, "temperature": 0.2,
    }).encode("utf-8")
    request = urllib.request.Request(
        config["model_chat_url"],
        data=payload,
        headers={
            "Authorization": "Bearer " + config["model_api_key"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=CHAT_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise ApiError(502, "provider_error", "The configured model provider returned an error.")
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ApiError(502, "provider_error", "The configured model provider returned an error.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiError(504, "provider_unavailable", "The configured model provider did not respond.") from exc
    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ApiError(502, "provider_response_too_large", "The model provider response exceeded the size limit.")
    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ApiError(502, "invalid_provider_response", "The model provider returned an invalid response.") from exc
    if not isinstance(content, str) or not content.strip():
        raise ApiError(502, "empty_provider_response", "The model provider returned no message.")
    return content.strip()[:MAX_MESSAGE_CHARS], model


def validate_ollama_vision_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "http" or not parsed.hostname
            or parsed.port != 11434
            or parsed.hostname not in ("127.0.0.1", "localhost", "::1", "ollama")
            or (parsed.hostname == "ollama" and os.environ.get("DEBIAN13_ALLOW_INTERNAL_OLLAMA") != "1")
            or parsed.path != "/v1/chat/completions"
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("OLLAMA_VISION_URL must target local Ollama port 11434.")
    return value


def complete_vision(profile: str, message: str, config: dict, image: str,
                    history: list[dict] | None = None,
                    notes: list[dict] | None = None) -> tuple[str, str, str]:
    """Always try private Ollama first. External fallback is explicit and optional.

    Images are sent to a paid/external API ONLY when VISION_FALLBACK_ENABLED=1,
    all fallback settings are provided, and the local attempt fails.
    """
    vision_url = config.get("ollama_vision_url") or "http://127.0.0.1:11434/v1/chat/completions"
    primary = dict(config,
                   model_chat_url=vision_url,
                   model_api_key="ollama-local-no-remote-secret",
                   model_name=config.get("ollama_vision_model") or "llama3.2-vision:11b",
                   vision_model_name=config.get("ollama_vision_model") or "llama3.2-vision:11b")
    try:
        answer, model = complete_chat(profile, message, primary, image, history, notes,
                                      ollama_image_format=True)
        return answer, model, "ollama"
    except ApiError as exc:
        if exc.code not in ("provider_unavailable", "provider_error", "provider_response_too_large",
                            "invalid_provider_response", "empty_provider_response"):
            raise
        if not config.get("vision_fallback_enabled", False):
            raise ApiError(503, "ollama_vision_unavailable",
                           "Ollama Vision is unavailable. External fallback is disabled.") from exc
        url = config.get("vision_fallback_url", "")
        key = config.get("vision_fallback_key", "")
        model = config.get("vision_fallback_model", "")
        if not url or not key or not model:
            raise ApiError(503, "vision_fallback_not_configured",
                           "Vision fallback was enabled but its credentials are missing.") from exc
        fallback = dict(config, model_chat_url=url, model_api_key=key,
                        model_name=model, vision_model_name=model)
        content, model_name = complete_chat(profile, message, fallback, image, history, notes)
        return content, model_name, "vision_fallback_api"


def runtime_diagnostics(server) -> dict:
    linux_security = {}
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
        for line in status.splitlines():
            name, separator, value = line.partition(":")
            if not separator:
                continue
            if name == "NoNewPrivs":
                linux_security["noNewPrivileges"] = value.strip() == "1"
            elif name == "Seccomp":
                linux_security["seccompMode"] = int(value.strip())
            elif name == "CapEff":
                linux_security["effectiveCapabilities"] = value.strip()
    except (OSError, ValueError):
        pass

    return {
        "service": "debian13-system",
        "pythonVersion": platform.python_version(),
        "effectiveUid": os.geteuid(),
        "effectiveGid": os.getegid(),
        "workspaceWritable": os.access(server.workspace, os.W_OK),
        "providerConfigured": bool(
            server.config["model_chat_url"]
            and server.config["model_api_key"]
            and server.config["model_name"]
        ),
        "linuxSecurity": linux_security,
    }


class Debian13Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], config: dict):
        self.config = config
        self.workspace = Path(config["workspace"]).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.work_store = WorkStore(self.workspace)
        self.github_dashboard = DashboardCache()
        self.ui_sync = SyncStore(self.workspace)
        self.chat_memory = ChatMemory(self.workspace)
        self._alex_dispatch_lock = threading.Lock()
        self._rates: dict[str, tuple[float, int]] = {}
        self._rates_lock = threading.Lock()
        super().__init__(address, SystemHandler)


class SystemHandler(http.server.BaseHTTPRequestHandler):
    server: Debian13Server
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        # Never log request bodies, authorization headers, or model credentials.
        super().log_message("%s %s", self.address_string(), self.path.split("?", 1)[0])

    def _send_json(self, status: int, data: dict, headers: dict | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_chat_events(self, content: str, profile: str, model: str,
                          session_id: str, source_override: str | None = None,
                          ui_revision: int | None = None) -> None:
        # A complete provider reply is delivered in one SSE delta (not token streaming).
        # Operator slash commands use the same transport but are NOT AI inference.
        source = source_override or (
            "ollama" if is_ollama_endpoint(self.server.config["model_chat_url"])
            else "model_provider")
        frames = [
            ("meta", {"model": model, "session_id": session_id, "session_persisted": True}),
            ("delta", {"text": content}),
        ]
        if ui_revision is not None:
            frames.append(("ui_update", {"revision": ui_revision,
                                         "source": "operator_command"}))
        frames.append(("done", {"agent": profile, "source": source, "model": model,
                                "session_id": session_id}))
        body = "".join(
            "event: " + event + "\n" +
            "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            for event, payload in frames
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorize(self) -> bool:
        expected = "Bearer " + self.server.config["api_token"]
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            self._send_json(401, {"error": {
                "code": "unauthorized",
                "message": "A valid bearer token is required.",
                "requestId": str(uuid.uuid4()),
            }})
            return False
        return True

    def _dialog_identity(self) -> str | None:
        """Identify a voluntary chat participant from its own bearer credential.

        Neither a client-supplied sender field nor an agent profile is proof
        of identity. Unconfigured agents cannot publish as AI.
        """
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1 or not values[0].startswith("Bearer "):
            return None
        supplied = values[0][7:].encode("utf-8")
        candidates = [("operator", self.server.config["api_token"])]
        for agent in PROFILES:
            secret = os.environ.get("DEBIAN13_DIALOG_" + agent.upper() + "_TOKEN", "")
            if len(secret) >= 32 and len(secret) <= 512 and all(33 <= ord(c) <= 126 for c in secret):
                candidates.append((agent, secret))
        matched = [name for name, secret in candidates
                   if hmac.compare_digest(supplied, secret.encode("utf-8"))]
        # Reused credentials would make identity ambiguous: fail closed.
        return matched[0] if len(matched) == 1 else None

    def _dialog_unauthorized(self) -> None:
        self._send_json(401, {"error": {"code": "dialog_unauthorized",
                                       "message": "Individual agent or operator token required."}})

    def _allow_request(self) -> bool:
        now = time.monotonic()
        address = self.client_address[0]
        with self.server._rates_lock:
            start, count = self.server._rates.get(address, (now, 0))
            if now - start >= RATE_WINDOW_SECONDS:
                start, count = now, 0
            if count >= self.server.config.get("rate_limit", RATE_LIMIT):
                retry = max(1, int(RATE_WINDOW_SECONDS - (now - start)))
                self._send_json(
                    429,
                    {"error": {
                        "code": "rate_limited",
                        "message": "Request rate limit exceeded.",
                        "requestId": str(uuid.uuid4()),
                    }},
                    {"Retry-After": str(retry)},
                )
                return False
            self.server._rates[address] = (start, count + 1)
            if len(self.server._rates) > 1024:
                self.server._rates = {
                    key: value for key, value in self.server._rates.items()
                    if now - value[0] < RATE_WINDOW_SECONDS
                }
        return True

    def _read_json(self, limit: int = MAX_BODY_BYTES) -> dict:
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdigit():
            raise ApiError(400, "invalid_content_length", "A valid Content-Length header is required.")
        if len(raw_length) > 8:
            raise ApiError(413, "request_too_large", "Request body exceeds 16 KiB.")
        length = int(raw_length)
        if length > limit:
            raise ApiError(413, "request_too_large", "Request body exceeds the size limit.")
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(400, "invalid_json", "Request body must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Request body must be a JSON object.")
        return data


    def _mcp_read_local(self, path: str) -> dict:
        # Read the same authenticated API via localhost, never a supplied URL.
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request("GET", path, headers={
                "Authorization": "Bearer " + self.server.config["api_token"],
                "Accept": "application/json",
            })
            reply = conn.getresponse()
            raw = reply.read(mcp_server.MAX_RESPONSE + 1)
            if reply.status != 200 or len(raw) > mcp_server.MAX_RESPONSE:
                raise ValueError("Local API unavailable")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Invalid local API response")
            return data
        finally:
            conn.close()

    def _handle_mcp(self) -> None:
        # Stateless MCP HTTP/JSON transport. Local bind is the safe default.
        origin = self.headers.get("Origin")
        allowed = {item.strip() for item in
                   os.environ.get("DEBIAN13_MCP_ALLOWED_ORIGINS", "").split(",")
                   if item.strip()}
        if origin and origin not in allowed:
            self._send_json(403, {"error": "MCP Origin not allowed"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            self._send_json(415, {"error": "MCP requires application/json"})
            return
        request = self._read_json(65536)
        if request.get("jsonrpc") != "2.0":
            self._send_json(400, {"jsonrpc": "2.0", "id": request.get("id"),
                                  "error": {"code": -32600, "message": "Invalid Request"}})
            return
        result = mcp_server.response(request, api_reader=self._mcp_read_local)
        if result is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("MCP-Protocol-Version", mcp_server.PROTOCOL_VERSION)
            self.end_headers()
        else:
            self._send_json(200, result,
                            {"MCP-Protocol-Version": mcp_server.PROTOCOL_VERSION})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/v1/agent-dialog/messages":
            if not self._allow_request():
                return
            identity = self._dialog_identity()
            if identity is None:
                self._dialog_unauthorized()
                return
            try:
                params = urllib.parse.parse_qs(parsed.query, strict_parsing=True, max_num_fields=1)
                if set(params) - {"after"} or any(len(v) != 1 for v in params.values()):
                    raise ValueError("unknown query parameter")
                raw_after = params.get("after", ["0"])[0]
                if not raw_after.isascii() or not raw_after.isdecimal() or len(raw_after) > 19:
                    raise ValueError("invalid cursor")
                after = int(raw_after)
                messages = self.server.chat_memory.get_agent_dialog(identity, after=after)
            except ValueError:
                self._send_json(400, {"error": {"code": "invalid_dialog_cursor",
                                               "message": "Use a nonnegative numeric after cursor."}})
                return
            self._send_json(200, {"messages": messages, "storage": "sqlite",
                                  "audience": identity, "lastId": messages[-1]["id"] if messages else after,
                                  "voluntary": True, "agentInferenceVerified": False})
            return
        if self.path == "/mcp":
            self._send_json(405, {"error": "Use MCP POST transport"}, {"Allow": "POST"})
            return
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if not self._allow_request():
            return
        if not self._authorize():
            return
        if self.path == "/v1/profiles":
            self._send_json(200, {
                "profiles": [
                    {"id": key, "name": value["name"], "role": None,
                     "assignment": None, "customInstructions": False, "active": False}
                    for key, value in PROFILES.items()
                ],
                "providerConfigured": bool(
                    self.server.config["model_chat_url"]
                    and self.server.config["model_api_key"]
                    and self.server.config["model_name"]
                ),
            })
            return
        if self.path == "/v1/diagnostics":
            self._send_json(200, runtime_diagnostics(self.server))
            return
        if self.path == "/v1/integrations":
            self._send_json(200, {"integrations": [
                {"id": "mcp", "name": "MCP", "status": "stdio_available",
                 "connected": False, "description": "Read-only MCP stdio bridge; client connection not verified."},
                {"id": "gpt_actions", "name": "GPT Actions", "status": "schema_available",
                 "connected": False, "description": "OpenAPI document available; HTTPS deployment and GPT setup required."},
                {"id": "codex", "name": "Codex", "status": "workflow_available",
                 "connected": False, "description": "GitHub Actions workflow; requires OPENAI_API_KEY and a verified run."},
            ]})
            return
        if self.path == "/api/v1/status":
            report = runtime_diagnostics(self.server)
            self._send_json(200, {
                "application": {"status": "running", "product": "Debian 13 Console"},
                "operating_system": {"runtime": {
                    "distribution": platform.platform(),
                    "debian_13_boot_verified": False,
                }},
                "model": {
                    "status": "configured" if report["providerConfigured"] else "not_configured",
                    "inference": "unverified",
                },
                "task_state": ("Worker Ollama: heartbeat potwierdzony" if self.server.work_store.local_worker_status()["workerHeartbeatVerified"] else "Worker Ollama: niepotwierdzony"),
                "svl": inspect_svl(self.server.workspace),
            })
            return
        if self.path == "/api/v1/ai-center/status":
            self._send_json(200, self.server.work_store.local_worker_status())
            return
        if self.path == "/api/v1/codex/status":
            self._send_json(200, codex_status_report())
            return
        if self.path == "/api/v1/svl/status":
            self._send_json(200, inspect_svl(self.server.workspace))
            return
        if self.path == "/api/v1/integration-progress":
            self._send_json(200, integration_progress_report(
                codex=codex_status_report(),
                ollama=self.server.work_store.local_worker_status(),
                original_agents=original_agents_report(),
                model_chat_url=self.server.config.get("model_chat_url", ""),
            ))
            return
        if self.path == "/api/v1/sync/state":
            self._send_json(200, self.server.ui_sync.snapshot())
            return
        if self.path == "/api/v1/github/status":
            try:
                snapshot = self.server.github_dashboard.get()
            except GitHubUnavailable:
                self._send_json(503, {"error": {
                    "code": "github_unavailable",
                    "message": "GitHub configuration or API unavailable."}})
                return
            self._send_json(200 if snapshot["githubApiReachable"] else 503, snapshot)
            return
        if self.path == "/api/v1/original-agents":
            self._send_json(200, original_agents_report())
            return
        if self.path == "/api/v1/agents":
            self._send_json(200, {"agents": [
                {"id": key, "name": profile["name"], "role": None,
                 "assignment": None, "customInstructions": False,
                 "status": "unassigned", "worker": "not_running"}
                for key, profile in PROFILES.items()
            ]})
            return
        if self.path == "/api/v1/tasks":
            live = self.server.work_store.local_worker_status()["workerHeartbeatVerified"]
            self._send_json(200, {"tasks": self.server.work_store.list_tasks(),
                                  "storage": "sqlite", "workersRunning": int(live)})
            return
        if self.path == "/api/v1/events":
            self._send_json(200, {"events": self.server.work_store.list_events(),
                                  "storage": "sqlite"})
            return
        if self.path == "/api/v1/original-agents/review-capabilities":
            try:
                status = original_review_capabilities()
            except OriginalAgentError:
                status = {"configured": True, "supported": False,
                          "agentRuntimeVerified": False, "reason": "source_unavailable"}
            self._send_json(200, {"originalAgents": status})
            return
        original_status_match = TASK_ORIGINAL_STATUS_PATH.fullmatch(self.path)
        if original_status_match:
            task = self.server.work_store.get_task(original_status_match.group(1))
            if not task:
                self._send_json(404, {"error": {"code": "task_not_found",
                                                "message": "Task not found."}})
                return
            submission = self.server.work_store.get_original_submission(task["id"])
            if not submission:
                self._send_json(404, {"error": {"code": "not_submitted",
                                                "message": "Task was not sent to original AI."}})
                return
            try:
                data = original_review_status(submission["remoteTaskId"], task["agent"])
            except OriginalAgentError:
                self._send_json(503, {"error": {
                    "code": "source_unavailable",
                    "message": "Original agent status is unavailable or invalid."}})
                return
            self._send_json(200, {"originalJob": data})
            return
        if self.path == "/api/v1/alex/capabilities":
            try:
                status = alex_capabilities()
            except (AlexHandoffError, ValueError):
                status = {"configured": True, "supported": False,
                          "agentRuntimeVerified": False, "reason": "alex_endpoint_unavailable"}
            self._send_json(200, {"alex": status})
            return
        session_match = SESSION_MESSAGES_PATH.fullmatch(self.path)
        if session_match:
            messages = self.server.chat_memory.get_messages(session_match.group(1))
            if messages is None:
                self._send_json(404, {"error": {"code": "session_not_found",
                                                 "message": "Session not found."}})
            else:
                self._send_json(200, {"session_id": session_match.group(1),
                                      "messages": messages, "storage": "sqlite"})
            return
        match = TASK_DETAIL_PATH.fullmatch(self.path)
        if match:
            task = self.server.work_store.get_task(match.group(1))
            if task is None:
                self._send_json(404, {"error": {"code": "task_not_found", "message": "Task not found."}})
            else:
                self._send_json(200, {
                    "task": task,
                    "localAnalysis": self.server.work_store.result_for_task(task["id"]),
                    "alexSubmission": self.server.work_store.get_alex_submission(task["id"]),
                    "originalSubmission": self.server.work_store.get_original_submission(task["id"])
                })
            return
        self._send_json(404, {"error": {"code": "not_found", "requestId": str(uuid.uuid4())}})

    def do_POST(self) -> None:
        if self.path == "/api/v1/agent-dialog/messages":
            if not self._allow_request():
                return
            identity = self._dialog_identity()
            if identity is None:
                self._dialog_unauthorized()
                return
            try:
                data = self._read_json()
                if set(data) != {"to", "content"}:
                    raise ValueError("Only to and content are accepted.")
                entry = self.server.chat_memory.post_agent_dialog(
                    identity, data["to"], data["content"])
            except ValueError:
                self._send_json(400, {"error": {"code": "invalid_dialog_message",
                                               "message": "Use a valid recipient and 1-4000 characters."}})
                return
            except ApiError as exc:
                self._send_json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
                return
            self._send_json(201, {"message": entry, "accepted": True,
                                  "agentInferenceVerified": False,
                                  "automaticReplyScheduled": False})
            return
        if not self._allow_request():
            return
        if not self._authorize():
            return
        request_id = str(uuid.uuid4())
        try:
            if self.path == "/mcp":
                self._handle_mcp()
                return
            approve = TASK_APPROVE_ANALYSIS_PATH.fullmatch(self.path)
            if approve:
                if self.headers.get("Content-Length", "0") not in ("", "0") or self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                    raise ApiError(400, "unexpected_body", "Local analysis approval takes no request body.")
                try:
                    task = self.server.work_store.approve_local_analysis(approve.group(1))
                except ValueError as exc:
                    raise ApiError(409, "task_not_eligible", "Task cannot be approved for local analysis.") from exc
                if task is None:
                    raise ApiError(404, "task_not_found", "Task not found.")
                self._send_json(202, {"task": task, "executionStarted": False,
                                      "localWorkerHeartbeatVerified":
                                        self.server.work_store.local_worker_status()["workerHeartbeatVerified"]})
                return
            original_handoff = TASK_ORIGINAL_HANDOFF_PATH.fullmatch(self.path)
            if original_handoff:
                if self.headers.get("Content-Length", "0") not in ("", "0") or self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                    raise ApiError(400, "unexpected_body",
                                   "Original agent handoff takes no request body.")
                task_id = original_handoff.group(1)
                with self.server._alex_dispatch_lock:
                    existing = self.server.work_store.get_original_submission(task_id)
                    if existing:
                        self._send_json(200, {"originalSubmission": existing,
                                              "executionStarted": False})
                        return
                    task = self.server.work_store.get_task(task_id)
                    if task is None:
                        raise ApiError(404, "task_not_found", "Task not found.")
                    if self.server.work_store.get_alex_submission(task_id):
                        raise ApiError(409, "already_handed_off",
                                       "Task already sent through legacy Alex API.")
                    receipt = submit_original_review(task)
                    submission = self.server.work_store.record_original_submission(
                        task_id, receipt["remoteTaskId"])
                self._send_json(202, {"originalSubmission": submission,
                                      "executionStarted": False,
                                      "agentExecutionVerified": False,
                                      "pullRequestVerified": False})
                return
            dispatch = TASK_DISPATCH_PATH.fullmatch(self.path)
            if dispatch:
                if self.headers.get("Content-Length", "0") not in ("", "0") or self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                    raise ApiError(400, "unexpected_body", "Task dispatch takes no request body.")
                task_id = dispatch.group(1)
                with self.server._alex_dispatch_lock:
                    existing = self.server.work_store.get_alex_submission(task_id)
                    if existing:
                        self._send_json(200, {"alexSubmission": existing,
                                              "executionStarted": False})
                        return
                    task = self.server.work_store.get_task(task_id)
                    if task is None:
                        raise ApiError(404, "task_not_found", "Task not found.")
                    receipt = submit_to_alex(task)
                    submission = self.server.work_store.record_alex_submission(
                        task_id, receipt["remoteTaskId"])
                self._send_json(202, {"alexSubmission": submission,
                                      "executionStarted": False,
                                      "agentExecutionVerified": False})
                return
            cancel = TASK_CANCEL_PATH.fullmatch(self.path)
            if cancel:
                if self.headers.get("Content-Length", "0") not in ("", "0") or self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                    raise ApiError(400, "unexpected_body", "Task cancellation takes no request body.")
                with self.server._alex_dispatch_lock:
                    if (self.server.work_store.get_alex_submission(cancel.group(1))
                            or self.server.work_store.get_original_submission(cancel.group(1))):
                        raise ApiError(409, "already_handed_off",
                                       "Task accepted by a remote AI API; local cancellation cannot recall it.")
                    task = self.server.work_store.cancel_task(cancel.group(1))
                if task is None:
                    raise ApiError(404, "task_not_found", "Task not found.")
                self._send_json(200, {"task": task})
                return
            data = self._read_json(MAX_VISION_BODY_BYTES if self.path == "/v1/vision" else MAX_BODY_BYTES)
            if self.path == "/api/v1/tasks":
                if set(data) != {"agent", "kind", "title", "description"}:
                    raise ApiError(400, "invalid_task", "Required: agent, kind, title, description.")
                agent, kind = data["agent"], data["kind"]
                title, description = data["title"], data["description"]
                if (not isinstance(agent, str) or agent not in PROFILES
                        or not isinstance(kind, str) or kind not in WORK_KINDS
                        or not isinstance(title, str) or not 1 <= len(title.strip()) <= 120
                        or not isinstance(description, str) or not 1 <= len(description.strip()) <= 2000):
                    raise ApiError(400, "invalid_task", "Invalid task fields or length.")
                try:
                    task = self.server.work_store.create_task(
                        kind, agent, title.strip(), description.strip())
                except ValueError as exc:
                    raise ApiError(409, "task_limit", "Task ledger is at capacity.") from exc
                self._send_json(201, {"task": task, "executionStarted": False})
                return
            if self.path in ("/v1/terminal", "/api/v1/terminal"):
                result = run_terminal(data.get("command"), self.server.workspace)
                if self.path == "/api/v1/terminal":
                    self._send_json(200, {"requestId": request_id, "output": result["stdout"] + result["stderr"],
                                          "exitCode": result["exitCode"]})
                else:
                    self._send_json(200, {"requestId": request_id, **result})
                return
            if self.path in ("/v1/chat", "/v1/vision", "/api/v1/chat/stream"):
                profile = data.get("agent") if self.path == "/api/v1/chat/stream" else data.get("profile")
                message = data.get("message")
                if not isinstance(profile, str) or profile not in PROFILES:
                    raise ApiError(400, "invalid_profile", "Select aria, astyria, aurelius, or alex.")
                if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
                    raise ApiError(400, "invalid_message", "Message must contain 1–4000 characters.")
                image = validate_image(data.get("image")) if self.path == "/v1/vision" else None
                session_id = data.get("session_id")
                if session_id is not None:
                    if not isinstance(session_id, str) or not re.fullmatch(TASK_UUID, session_id):
                        raise ApiError(400, "invalid_session", "Invalid session ID.")
                    if not self.server.chat_memory.exists(session_id):
                        raise ApiError(404, "session_not_found", "Session not found.")
                clean_message = message.strip()
                if image is None and clean_message.casefold() in ("/status", "/integracje", "/postep", "/svl"):
                    # Explicit operator commands: deterministic, read-only, no LLM or shell.
                    if clean_message.casefold() == "/status":
                        diagnostic = runtime_diagnostics(self.server)
                        local = self.server.work_store.local_worker_status()
                        content = (
                            "Status rzeczywistego API Debian 13: aktywne.\n"
                            f"Provider modelu: {'skonfigurowany (bez potwierdzenia inferencji)' if diagnostic['providerConfigured'] else 'nieskonfigurowany'}.\n"
                            f"Worker lokalny Ollama: {'heartbeat aktualny' if local['workerHeartbeatVerified'] else 'brak potwierdzonego heartbeat'}.\n"
                            "Oryginalni agenci i start pełnego systemu Debian 13: niezweryfikowane."
                        )
                    elif clean_message.casefold() == "/svl":
                        svl = inspect_svl(self.server.workspace)
                        content = (
                            "SVL Python: wersja " + str(svl.get("version", "nieznana")) + "\n"
                            + "Magazyn stanu: " + ("dostępny" if svl.get("storePresent") else "tylko ustawienia domyślne") + "\n"
                            + "Historia: " + str(svl.get("chatHistoryEntries", 0)) + " wiadomości, "
                            + str(svl.get("updateEntries", 0)) + " aktualizacji.\n"
                            + "To starszy silnik stanu SVL; nie uruchamia jądra Debian, root ani agentów AI."
                        ) if svl.get("stateValid") else "SVL: magazyn nieprawidłowy lub niedostępny; bez zmian danych."
                    else:
                        progress = integration_progress_report(
                            codex=codex_status_report(),
                            ollama=self.server.work_store.local_worker_status(),
                            original_agents=original_agents_report(),
                            model_chat_url=self.server.config.get("model_chat_url", ""),
                        )
                        lines = [
                            f"Integracje: {progress['percentage']}% ({progress['verifiedCount']}/{progress['totalCount']} potwierdzonych kontroli)."
                        ]
                        for item in progress["modules"]:
                            lines.append(
                                f"{item['name']}: {item['percentage']}% ({item['verifiedCount']}/{item['totalCount']})."
                            )
                        lines.append("To nie jest procent gotowości OS ani potwierdzenie działania agentów AI.")
                        content = "\n".join(lines)
                    session_id = self.server.chat_memory.save_exchange(
                        session_id, profile, clean_message, content, "system_command")
                    if self.path == "/api/v1/chat/stream":
                        self._send_chat_events(
                            content, profile, "none", session_id,
                            source_override="system_command")
                    else:
                        self._send_json(200, {
                            "requestId": request_id, "profile": profile, "content": content,
                            "model": None, "source": "system_command", "sessionId": session_id,
                        })
                    return
                if image is None:
                    try:
                        new_title = parse_title_command(clean_message)
                        if new_title is not None:
                            synced = self.server.ui_sync.set_title(new_title)
                            content = ("Zapisano tytuł interfejsu: " + synced["title"]
                                       + ". Zmiana zostanie zsynchronizowana we "
                                       "wszystkich otwartych panelach. "
                                       "To polecenie systemowe, nie odpowiedź modelu AI.")
                            session_id = self.server.chat_memory.save_exchange(
                                session_id, profile, clean_message, content, "system_command")
                            if self.path == "/api/v1/chat/stream":
                                self._send_chat_events(
                                    content, profile, "none", session_id,
                                    source_override="system_command",
                                    ui_revision=synced["revision"])
                            else:
                                self._send_json(200, {
                                    "requestId": request_id, "profile": profile,
                                    "content": content, "model": None,
                                    "source": "system_command", "sessionId": session_id,
                                    "uiRevision": synced["revision"]})
                            return
                    except SyncValidationError as exc:
                        raise ApiError(400, "invalid_ui_command", str(exc)) from exc
                history = self.server.chat_memory.context(session_id)
                notes = self.server.chat_memory.notes()
                if image is not None:
                    content, model, source = complete_vision(
                        profile, clean_message, self.server.config, image,
                        history=history, notes=notes)
                else:
                    content, model = complete_chat(profile, clean_message, self.server.config,
                                                   history=history, notes=notes)
                    source = ("ollama" if is_ollama_endpoint(self.server.config["model_chat_url"])
                              else "model_provider")
                session_id = self.server.chat_memory.save_exchange(
                    session_id, profile, clean_message, content, source, image_attached=image is not None)
                if self.path == "/api/v1/chat/stream":
                    self._send_chat_events(content, profile, model, session_id)
                    return
                self._send_json(200, {
                    "requestId": request_id, "profile": profile, "content": content,
                    "model": model, "source": source, "sessionId": session_id,
                })
                return
            self._send_json(404, {"error": {"code": "not_found", "requestId": request_id}})
        except ApiError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": str(exc), "requestId": request_id}})
        except OriginalAgentError as exc:
            code = 409 if exc.code in ("invalid_task", "unsupported_protocol",
                                      "unverified_receipt") else 503
            self._send_json(code, {"error": {"code": exc.code, "message": str(exc),
                                              "requestId": request_id}})
        except AlexHandoffError as exc:
            code = 409 if exc.code in ("invalid_task", "task_already_executed",
                                       "dispatch_not_enabled", "unsupported_capability",
                                       "submission_unverified") else 503
            self._send_json(code, {"error": {"code": exc.code, "message": str(exc),
                                             "requestId": request_id}})
        except ValueError:
            self._send_json(409, {"error": {"code": "dispatch_conflict",
                                             "message": "Task cannot be dispatched.",
                                             "requestId": request_id}})
        except (BrokenPipeError, ConnectionResetError):
            return


def load_config() -> dict:
    token = os.environ.get("DEBIAN13_API_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("DEBIAN13_API_TOKEN must contain at least 32 characters.")
    try:
        provider_url = validate_provider_url(os.environ.get("MODEL_CHAT_URL", "").strip())
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        ollama_vision_url = validate_ollama_vision_url(
            os.environ.get("OLLAMA_VISION_URL",
                           "http://127.0.0.1:11434/v1/chat/completions").strip())
        fallback_enabled = os.environ.get("VISION_FALLBACK_ENABLED", "0") == "1"
        fallback_url = validate_provider_url(os.environ.get("VISION_FALLBACK_CHAT_URL", "").strip())
        fallback_key = os.environ.get("VISION_FALLBACK_API_KEY", "")
        fallback_model = os.environ.get("VISION_FALLBACK_MODEL", "").strip()
        if fallback_enabled and (not fallback_url or not fallback_key or not fallback_model):
            raise ValueError("Vision fallback requires URL, API key and model; or disable fallback.")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return {
        "api_token": token,
        "ollama_vision_url": ollama_vision_url,
        "ollama_vision_model": os.environ.get("OLLAMA_VISION_MODEL", "llama3.2-vision:11b"),
        "vision_fallback_enabled": fallback_enabled,
        "vision_fallback_url": fallback_url,
        "vision_fallback_key": fallback_key,
        "vision_fallback_model": fallback_model,
        "model_chat_url": provider_url,
        "model_api_key": os.environ.get("MODEL_API_KEY", ""),
        "model_name": os.environ.get("MODEL_NAME", ""),
        "vision_model_name": os.environ.get("VISION_MODEL_NAME", ""),
        "workspace": os.environ.get("WORKSPACE_DIR", "/workspace"),
        "host": os.environ.get("HOST", "127.0.0.1"),
        "port": int(os.environ.get("PORT", "8080")),
    }


def main() -> None:
    config = load_config()
    server = Debian13Server((config["host"], config["port"]), config)
    print(f"Debian 13 system API listening on {config['host']}:{config['port']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
