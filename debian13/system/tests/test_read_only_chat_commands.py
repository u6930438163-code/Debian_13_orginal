"""Operator can inspect runtime without Ollama or any autonomous agent."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Debian13Server

TOKEN = "read-only-operator-commands-token-32-characters"


class OfflineOperatorCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = Debian13Server(("127.0.0.1", 0), {
            "api_token": TOKEN, "model_chat_url": "",
            "model_api_key": "", "model_name": "",
            "workspace": self.tmp.name,
        })
        self.worker = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.api.shutdown()
        self.api.server_close()
        self.worker.join(timeout=3)
        self.tmp.cleanup()

    def post(self, path, message, *, token=TOKEN):
        conn = http.client.HTTPConnection(*self.api.server_address, timeout=4)
        try:
            headers = {"Content-Type": "application/json"}
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            conn.request("POST", path, headers=headers,
                         body=json.dumps({"profile": "aria", "agent": "aria",
                                          "message": message}))
            response = conn.getresponse()
            status = response.status
            content = response.read().decode()
            return status, content
        finally:
            conn.close()

    def test_status_is_real_local_command_without_model(self):
        status, text = self.post("/v1/chat", "/status")
        self.assertEqual(status, 200)
        result = json.loads(text)
        self.assertEqual(result["source"], "system_command")
        self.assertIsNone(result["model"])
        self.assertIn("nieskonfigurowany", result["content"])
        self.assertIn("\n", result["content"])
        self.assertNotIn(TOKEN, result["content"])
        self.assertTrue(result["sessionId"])

    def test_progress_command_reads_checkpoints_without_inference(self):
        for command in ("/integracje", "/postep"):
            with self.subTest(command=command):
                status, text = self.post("/v1/chat", command)
                self.assertEqual(status, 200)
                result = json.loads(text)
                self.assertEqual(result["source"], "system_command")
                self.assertIn("Integracje: 30%", result["content"])
                self.assertIn("MCP: 50%", result["content"])
                self.assertIn("Ollama lokalna: 25%", result["content"])
                self.assertIn("To nie jest procent gotowości OS", result["content"])

    def test_stream_command_identifies_system_command_as_not_ai(self):
        status, text = self.post("/api/v1/chat/stream", "/status")
        self.assertEqual(status, 200)
        self.assertIn("event: done", text)
        self.assertIn('"source": "system_command"', text)
        self.assertIn("event: delta", text)

    def test_auth_and_unsupported_commands_fail_closed(self):
        status, _ = self.post("/v1/chat", "/status", token=None)
        self.assertEqual(status, 401)
        status, text = self.post("/v1/chat", "/not-a-local-command")
        self.assertEqual(status, 503)
        self.assertNotIn('"source": "system_command"', text)


if __name__ == "__main__":
    unittest.main()
