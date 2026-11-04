#!/usr/bin/env python3
"""Loopback-only static UI server with a narrow proxy to the Debian 13 API."""

from __future__ import annotations

import http.client
import json
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

UI_DIRECTORY = Path(__file__).resolve().parent
API_PATHS = {
    ("GET", "/api/v1/status"),
    ("GET", "/api/v1/agents"),
    ("GET", "/api/v1/original-agents"),
    ("GET", "/api/v1/original-agents/review-capabilities"),
    ("GET", "/api/v1/github/status"),
    ("GET", "/api/v1/sync/state"),
    ("GET", "/api/v1/codex/status"),
    ("GET", "/api/v1/ai-center/status"),
    ("GET", "/api/v1/integrations"),
    ("GET", "/api/v1/integration-progress"),
    ("GET", "/api/v1/svl/status"),
    ("GET", "/api/v1/alex/capabilities"),
    ("GET", "/api/v1/tasks"),
    ("POST", "/api/v1/tasks"),
    ("GET", "/api/v1/events"),
    ("GET", "/api/v1/agent-dialog/messages"),
    ("POST", "/api/v1/agent-dialog/messages"),
    ("POST", "/api/v1/chat"),
    ("POST", "/api/v1/chat/stream"),
    ("POST", "/api/v1/vision"),
    ("POST", "/api/v1/terminal"),
}
API_TASK_PATH = re.compile(
    r"/api/v1/tasks/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
)
API_ORIGINAL_HANDOFF_PATH = re.compile(
    r"/api/v1/tasks/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/handoff",
)
API_ORIGINAL_STATUS_PATH = re.compile(
    r"/api/v1/tasks/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/original-status",
)
API_TASK_CANCEL_PATH = re.compile(
    r"/api/v1/tasks/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/cancel",
)
API_TASK_APPROVE_ANALYSIS_PATH = re.compile(
    r"/api/v1/tasks/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/approve-analysis",
)
API_TASK_DISPATCH_PATH = re.compile(
    r"/api/v1/tasks/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/dispatch",
)
API_SESSION_PATH = re.compile(
    r"/api/v1/sessions/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/messages",
)
API_STREAM_PATH = "/api/v1/chat/stream"
MAX_PROXY_BODY_BYTES = 16_384
MAX_VISION_BODY_BYTES = 3 * 1024 * 1024
MAX_PROXY_RESPONSE_BYTES = 1_048_576
MAX_STREAM_SECONDS = 120
STREAM_CHUNK_BYTES = 65_536


class UiHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIRECTORY), **kwargs)

    def end_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _send_error_json(self, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_api(self) -> None:
        path = urlsplit(self.path).path
        # Allow only the cursor query parameter on the private chat read endpoint.
        query = urlsplit(self.path).query
        if query and (path != "/api/v1/agent-dialog/messages" or self.command != "GET"
                      or not re.fullmatch(r"after=[0-9]{1,19}", query)):
            self._send_error_json(400, "Nieprawidłowy parametr zapytania.")
            return
        if (self.command, path) not in API_PATHS and not (
            self.command == "GET" and (API_SESSION_PATH.fullmatch(path) or API_TASK_PATH.fullmatch(path)
                                              or API_ORIGINAL_STATUS_PATH.fullmatch(path))
        ) and not (
            self.command == "POST" and (API_TASK_CANCEL_PATH.fullmatch(path) or API_TASK_DISPATCH_PATH.fullmatch(path)
                                               or API_TASK_APPROVE_ANALYSIS_PATH.fullmatch(path)
                                               or API_ORIGINAL_HANDOFF_PATH.fullmatch(path))
        ):
            self._send_error_json(404, "Nie znaleziono endpointu API.")
            return

        body = None
        if self.command == "POST":
            if self.headers.get("Transfer-Encoding"):
                self._send_error_json(400, "Kodowanie transferu nie jest obsługiwane.")
                return
            raw_length = self.headers.get("Content-Length", "")
            if (API_TASK_CANCEL_PATH.fullmatch(path) or API_TASK_DISPATCH_PATH.fullmatch(path)
                    or API_TASK_APPROVE_ANALYSIS_PATH.fullmatch(path)
                    or API_ORIGINAL_HANDOFF_PATH.fullmatch(path)):
                if raw_length not in ("", "0"):
                    self._send_error_json(400, "Anulowanie zadania nie przyjmuje treści.")
                    return
                body = None
            else:
                try:
                    length = int(raw_length)
                except ValueError:
                    self._send_error_json(400, "Nieprawidłowy Content-Length.")
                    return
                max_bytes = MAX_VISION_BODY_BYTES if path == "/api/v1/vision" else MAX_PROXY_BODY_BYTES
                if length < 1 or length > max_bytes:
                    self._send_error_json(413, "Żądanie przekracza limit rozmiaru.")
                    return
                body = self.rfile.read(length)

        try:
            api_port = int(os.environ.get("DEBIAN13_API_PORT", "8765"))
            if not 1 <= api_port <= 65535:
                raise ValueError("Invalid API port")
            timeout = (MAX_STREAM_SECONDS if path in (API_STREAM_PATH, "/api/v1/vision")
                       else 15 if (path == "/api/v1/integration-progress"
                                   or path == "/api/v1/github/status"
                                   or path == "/api/v1/original-agents/review-capabilities"
                                   or API_ORIGINAL_HANDOFF_PATH.fullmatch(path)
                                   or API_ORIGINAL_STATUS_PATH.fullmatch(path))
                       else 5)
            api_host = os.environ.get("DEBIAN13_API_HOST", "127.0.0.1")
            if api_host not in ("127.0.0.1", "localhost") and not (
                api_host == "debian13-system"
                and os.environ.get("DEBIAN13_ALLOW_INTERNAL_API") == "1"
            ):
                raise ValueError("Untrusted internal API hostname")
            connection = http.client.HTTPConnection(api_host, api_port, timeout=timeout)
            headers = {}
            token = os.environ.get("DEBIAN13_API_TOKEN", "")
            if token:
                if any(ord(char) <= 32 or ord(char) == 127 for char in token):
                    raise ValueError("Invalid API token")
                headers["Authorization"] = "Bearer " + token
            if self.command == "POST" and body is not None:
                headers["Content-Type"] = "application/json"
            upstream_path = {"/api/v1/integrations": "/v1/integrations",
                             "/api/v1/vision": "/v1/vision"}.get(path, path)
            if path == "/api/v1/agent-dialog/messages" and query:
                upstream_path += "?" + query
            connection.request(self.command, upstream_path, body=body, headers=headers)
            response = connection.getresponse()
            if (
                path == API_STREAM_PATH
                and response.status == 200
                and response.getheader("Content-Type", "").lower().startswith("text/event-stream")
            ):
                self.send_response(response.status)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                total_bytes = 0
                try:
                    while True:
                        chunk = response.read1(STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > MAX_PROXY_RESPONSE_BYTES:
                            limit_event = (
                                b'event: error\ndata: {"error":"API response exceeds its size limit."}\n\n'
                            )
                            self.wfile.write(limit_event)
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError, http.client.HTTPException):
                    pass
                finally:
                    connection.close()
                return
            payload = response.read(MAX_PROXY_RESPONSE_BYTES + 1)
            status = response.status
            connection.close()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError, ValueError, http.client.HTTPException):
            try:
                connection.close()
            except (UnboundLocalError, OSError):
                pass
            self._send_error_json(502, "Runtime API Debian 13 jest niedostępne.")
            return

        if len(payload) > MAX_PROXY_RESPONSE_BYTES:
            self._send_error_json(502, "Odpowiedź API przekracza limit rozmiaru.")
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if urlsplit(self.path).path.startswith("/api/"):
            self._proxy_api()
            return
        super().do_GET()

    def do_POST(self) -> None:
        self._proxy_api()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    port = int(os.environ.get("DEBIAN13_UI_PORT", "4174"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid UI port")
    host = os.environ.get("DEBIAN13_UI_HOST", "127.0.0.1")
    if host != "127.0.0.1" and not (
        host == "0.0.0.0" and os.environ.get("DEBIAN13_ALLOW_INTERNAL_API") == "1"
    ):
        raise ValueError("Unsafe UI binding: use private Docker port exposure")
    server = ThreadingHTTPServer((host, port), UiHandler)
    print(f"Debian 13 UI listening on {host}:{port} (trusted internal API proxy only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
