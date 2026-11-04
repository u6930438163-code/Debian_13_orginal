"""Tests for the private Debian13 Connect stdio-to-HTTP bridge.

Use a real loopback Debian13 API; do not contact ChatGPT, Ollama, or remote agents.
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

SYSTEM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SYSTEM))
from server import Debian13Server

BRIDGE_FILE = SYSTEM.parent / "plugins" / "debian13-connect" / "bridge.py"
spec = importlib.util.spec_from_file_location("debian13_connect_bridge", BRIDGE_FILE)
assert spec is not None and spec.loader is not None
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

TOKEN = "private-mcp-bridge-test-token-longer-than-32"


class Debian13ConnectBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = Debian13Server(("127.0.0.1", 0), {
            "api_token": TOKEN,
            "model_chat_url": "",
            "model_api_key": "",
            "model_name": "",
            "workspace": self.temp.name,
        })
        self.thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.api.shutdown()
        self.api.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def forward(self, payload, *, port=None, token=TOKEN):
        with patch.dict(os.environ, {
            "DEBIAN13_API_PORT": str(port or self.api.server_port),
            "DEBIAN13_API_TOKEN": token,
        }):
            return bridge.forward(payload)

    def test_initialize_list_and_real_read_only_tool(self):
        initialized = self.forward({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        })
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
        listed = self.forward({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {item["name"] for item in listed["result"]["tools"]}
        self.assertIn("debian13_original_agents", tools)
        self.assertIn("debian13_profiles", tools)
        self.assertNotIn("run_shell", tools)
        response = self.forward({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "debian13_profiles", "arguments": {}},
        })
        self.assertFalse(response["result"]["isError"])
        profiles = json.loads(response["result"]["content"][0]["text"])["profiles"]
        self.assertEqual(len(profiles), 4)
        self.assertTrue(all(not entry["active"] for entry in profiles))
        self.assertNotIn(TOKEN, json.dumps(response))

    def test_invalid_token_or_port_is_rejected(self):
        request = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
        with self.assertRaises(ValueError):
            self.forward(request, token="short")
        with patch.dict(os.environ, {
            "DEBIAN13_API_PORT": "70000", "DEBIAN13_API_TOKEN": TOKEN,
        }):
            with self.assertRaises(ValueError):
                bridge.forward(request)

    def test_redirect_must_not_forward_bearer_token(self):
        received = []

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"jsonrpc":"2.0","id":5,"result":{}}')

            do_POST = do_GET

            def log_message(self, *args):
                pass

        sink = HTTPServer(("127.0.0.1", 0), Sink)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/mcp")
                self.end_headers()

            def log_message(self, *args):
                pass

        redirect = HTTPServer(("127.0.0.1", 0), Redirect)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            with self.assertRaises(HTTPError) as error:
                self.forward({"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                             port=redirect.server_port)
            self.assertEqual(error.exception.code, 302)
            self.assertEqual(received, [])
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join(timeout=3)
            sink.shutdown()
            sink.server_close()
            sink_thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
