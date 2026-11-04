"""Conservative, reproducible integration percentages; no external services."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SYSTEM))
from integration_progress import report
from server import Debian13Server

TOKEN = "progress-test-token-longer-than-thirty-two-chars"


class PercentageTests(unittest.TestCase):
    def test_unconfigured_is_not_marked_as_connected(self):
        result = report(codex={}, ollama={}, original_agents={})
        self.assertEqual(result["percentage"], 30)
        self.assertEqual(result["verifiedCount"], 6)
        self.assertEqual(result["totalCount"], 20)
        self.assertEqual(len(result["modules"]), 5)
        self.assertTrue(all(not item["connected"] for item in result["modules"]))
        self.assertTrue(result["notOsCompletion"])
        self.assertTrue(result["notDeploymentProof"])

    def test_registry_entries_and_heartbeat_do_not_forge_original_agent_execution(self):
        result = report(
            codex={"cliInstalled": True, "authentication": "not_authenticated",
                   "cloudWorkerVerified": False},
            ollama={"workerHeartbeatVerified": True, "executionVerified": False},
            original_agents={"registryConfigured": True, "registryReachable": True,
                             "runningEntriesInRegistry": 3,
                             "originalAgentExecutionVerified": False},
            model_chat_url="http://127.0.0.1:11434/v1/chat/completions",
        )
        items = {item["id"]: item for item in result["modules"]}
        self.assertEqual(items["original_agents"]["percentage"], 75)
        self.assertFalse(items["original_agents"]["connected"])
        self.assertEqual(items["ollama"]["percentage"], 75)
        self.assertFalse(items["ollama"]["connected"])
        self.assertFalse(items["codex"]["connected"])
        self.assertFalse(items["mcp"]["connected"])

    def test_external_model_urls_do_not_count_as_private_ollama(self):
        for url in (
            "https://api.openai.com/v1/chat/completions",
            "http://evil.example:11434/v1/chat/completions",
            "http://127.0.0.1:11434/v1/chat/completions?key=secret",
            "http://user:pass@localhost:11434/v1/chat/completions",
        ):
            with self.subTest(url=url):
                value = report(codex={}, ollama={}, original_agents={},
                               model_chat_url=url)
                ollama = next(x for x in value["modules"] if x["id"] == "ollama")
                self.assertEqual(ollama["percentage"], 25)

    def test_authenticated_api_exposes_evidence_not_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = Debian13Server(("127.0.0.1", 0), {
                "api_token": TOKEN, "model_chat_url": "",
                "model_api_key": "", "model_name": "", "workspace": tmp,
            })
            worker = threading.Thread(target=api.serve_forever, daemon=True)
            worker.start()
            try:
                conn = http.client.HTTPConnection(*api.server_address, timeout=4)
                try:
                    conn.request("GET", "/api/v1/integration-progress")
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 401)
                    resp.read()
                    conn.request("GET", "/api/v1/integration-progress",
                                 headers={"Authorization": "Bearer " + TOKEN})
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    result = json.load(resp)
                    self.assertEqual(result["percentage"], 30)
                    self.assertEqual(result["verifiedCount"], 6)
                    self.assertNotIn(TOKEN, json.dumps(result))
                    self.assertTrue(all(not item["connected"] for item in result["modules"]))
                finally:
                    conn.close()
            finally:
                api.shutdown()
                api.server_close()
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
