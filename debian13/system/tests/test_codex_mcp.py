"""Regression tests for separate Codex status API and MCP preflight.

No real Codex login, network connection, paid provider, or worker required.
"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Debian13Server
import codex_status
import mcp_autoconfigure
import mcp_launcher

TOKEN = "codex-test-secret-token-at-least-32-characters"


class CodexAndMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Debian13Server(("127.0.0.1", 0), {
            "api_token": TOKEN, "model_chat_url": "",
            "model_api_key": "", "model_name": "",
            "workspace": self.temp.name,
        })
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, body=None, token=TOKEN):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else {}
        finally:
            conn.close()

    def test_codex_status_requires_auth_and_does_not_invent_worker(self):
        status, _ = self.request("GET", "/api/v1/codex/status", token=None)
        self.assertEqual(status, 401)
        with patch.object(codex_status.shutil, "which", return_value=None):
            status, data = self.request("GET", "/api/v1/codex/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["module"], "codex")
        self.assertFalse(data["cliInstalled"])
        self.assertFalse(data["cloudWorkerVerified"])
        self.assertEqual(data["authentication"], "not_installed")
        self.assertNotIn(TOKEN, json.dumps(data))

    def test_mcp_http_auth_and_initialize_tools_and_read(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "test", "version": "1"}}}
        status, _ = self.request("POST", "/mcp", init, token=None)
        self.assertEqual(status, 401)
        status, data = self.request("POST", "/mcp", init)
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["protocolVersion"], "2025-11-25")
        status, data = self.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual(status, 200)
        self.assertTrue(any(tool["name"] == "debian13_profiles" for tool in data["result"]["tools"]))
        status, data = self.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "debian13_profiles", "arguments": {}}})
        self.assertEqual(status, 200)
        self.assertFalse(data["result"]["isError"])

    def test_auto_configuration_checks_real_local_endpoint(self):
        with patch.object(mcp_autoconfigure, "get_token", return_value=TOKEN):
            with patch.dict("os.environ", {"DEBIAN13_API_PORT": str(self.server.server_port)}):
                data = mcp_autoconfigure.check()
        self.assertTrue(data["mcpHttpVerified"])
        self.assertGreaterEqual(data["toolsAvailable"], 7)

    def test_private_token_mcp_launcher_rejects_short_token(self):
        with patch.dict("os.environ", {"DEBIAN13_API_TOKEN": "short"}):
            with self.assertRaises(ValueError):
                mcp_launcher.get_token()


if __name__ == "__main__":
    unittest.main()
