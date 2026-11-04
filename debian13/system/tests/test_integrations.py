"""Focused MCP and GPT Actions contract tests (no external model or account)."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SYSTEM))
from server import Debian13Server
import mcp_server

TOKEN = "integration-tests-token-with-32-characters"


class IntegrationTests(unittest.TestCase):
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

    def test_integration_route_requires_bearer_token(self):
        connection = http.client.HTTPConnection(*self.api.server_address, timeout=4)
        try:
            connection.request("GET", "/v1/integrations")
            unauthorized = connection.getresponse()
            self.assertEqual(unauthorized.status, 401)
            unauthorized.read()
            connection.request("GET", "/v1/integrations", headers={"Authorization": "Bearer " + TOKEN})
            reply = connection.getresponse()
            self.assertEqual(reply.status, 200)
            body = json.load(reply)
            self.assertEqual({item["id"] for item in body["integrations"]},
                             {"codex", "mcp", "gpt_actions"})
            self.assertTrue(all(item["connected"] is False for item in body["integrations"]))
        finally:
            connection.close()

    def test_mcp_stdio_initialize_tools_and_real_authenticated_read(self):
        initialized = mcp_server.response({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}
        })
        self.assertEqual(initialized["result"]["protocolVersion"], mcp_server.PROTOCOL_VERSION)
        listed = mcp_server.response({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({tool["name"] for tool in listed["result"]["tools"]},
                         {"debian13_profiles", "debian13_diagnostics", "debian13_integrations",
                          "debian13_ai_center", "debian13_codex_status",
                          "alex_existing_status", "alex_existing_tasks", "debian13_work_tasks",
                          "debian13_original_agents"})
        with patch.dict(os.environ, {
            "DEBIAN13_SYSTEM_API_URL": f"http://127.0.0.1:{self.api.server_port}",
            "DEBIAN13_API_TOKEN": TOKEN,
        }):
            called = mcp_server.response({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "debian13_profiles", "arguments": {}},
            })
        self.assertFalse(called["result"]["isError"])
        payload = json.loads(called["result"]["content"][0]["text"])
        self.assertEqual(len(payload["profiles"]), 4)
        self.assertTrue(all(p["active"] is False for p in payload["profiles"]))

    def test_mcp_rejects_unknown_and_mutating_tools(self):
        result = mcp_server.response({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "run_shell", "arguments": {"command": "id"}},
        })
        self.assertEqual(result["error"]["code"], -32602)

    def test_mcp_rejects_untrusted_http_and_auth_failures_without_echoing_secret(self):
        with patch.dict(os.environ, {"DEBIAN13_SYSTEM_API_URL": "http://evil.example:8080",
                                     "DEBIAN13_API_TOKEN": TOKEN}):
            reply = mcp_server.response({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "debian13_integrations", "arguments": {}},
            })
        self.assertTrue(reply["result"]["isError"])
        self.assertNotIn(TOKEN, json.dumps(reply))

    def test_gpt_actions_schema_documents_existing_routes_only(self):
        schema = json.loads((SYSTEM / "gpt-actions.openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["openapi"], "3.0.3")
        for route in ("/v1/profiles", "/v1/diagnostics", "/v1/chat", "/v1/integrations"):
            self.assertIn(route, schema["paths"])
        self.assertEqual(schema["security"], [{"bearerAuth": []}])
        self.assertTrue(schema["paths"]["/v1/chat"]["post"]["x-openai-isConsequential"])


if __name__ == "__main__":
    unittest.main()
