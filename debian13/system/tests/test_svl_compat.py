"""Real source-level SVL Python integration with Debian13; no fake OS boot."""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SYSTEM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SYSTEM))
from svl_compat import inspect_svl
from server import Debian13Server

TOKEN = "svl-compat-test-private-token-longer-than-32-chars"


class SVLCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)

    def test_real_original_python_engine_executes_without_ai(self):
        store = self.workspace / "original_svl.json"
        env = {
            "SVL_KERNEL_STORE_PATH": str(store),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
        }
        executed = subprocess.run(
            [sys.executable, "-I", str(SYSTEM / "legacy_svl_kernel.py"),
             "--action", "state"],
            check=False, capture_output=True, text=True, env=env, timeout=5,
        )
        self.assertEqual(executed.returncode, 0, executed.stderr)
        data = json.loads(executed.stdout)
        self.assertIn("9.0.0-Triada-BestOS-Mission", data["sys_version"])
        self.assertEqual(data["store"]["chat_history"], [])
        self.assertFalse(store.exists(), "Read-only action must not create data")

    def test_missing_store_is_not_falsely_reported_as_running_os(self):
        status = inspect_svl(self.workspace)
        self.assertTrue(status["sourceAvailable"])
        self.assertTrue(status["stateValid"])
        self.assertFalse(status["storePresent"])
        self.assertEqual(status["storeType"], "defaults_only")
        self.assertFalse(status["operatingSystemBootVerified"])
        self.assertFalse(status["profilesAreActiveWorkers"])
        self.assertTrue(status["readOnly"])
        self.assertFalse((self.workspace / ".svl_kernel_store.json").exists())

    def test_svl_legacy_migration_is_in_memory_not_disk(self):
        store = self.workspace / ".svl_kernel_store.json"
        payload = {
            "config": {
                "sys_version": "5.6.0-QuantumWheel",
                "mode": "Ollama-Free",
                "ai_model": "secret-provider-name",
                "operator_secret": "sensitive-config-must-not-leak",
                "active_profiles": ["Alex", "si_1", "Astyria", "Aurelius"],
            },
            "chat_history": [{"content": "secret-user-chat-not-for-ui"}],
            "update_history": [{"description": "upgrade"}],
        }
        store.write_text(json.dumps(payload), encoding="utf-8")
        before = store.read_bytes()
        state = inspect_svl(self.workspace)
        self.assertTrue(state["configMigratedInMemory"])
        self.assertTrue(state["storePresent"])
        self.assertEqual(state["version"], "5.6.0-QuantumWheel")
        self.assertEqual(state["chatHistoryEntries"], 1)
        self.assertEqual(state["updateEntries"], 1)
        self.assertNotIn("secret-user-chat-not-for-ui", json.dumps(state))
        self.assertNotIn("sensitive-config-must-not-leak", json.dumps(state))
        self.assertEqual(store.read_bytes(), before)

    def test_symlink_and_malformed_state_fail_closed(self):
        outside = self.workspace / "outside"
        outside.write_text("secret", encoding="utf-8")
        store = self.workspace / ".svl_kernel_store.json"
        store.symlink_to(outside)
        self.assertFalse(inspect_svl(self.workspace)["stateValid"])
        self.assertEqual(outside.read_text(), "secret")
        store.unlink()
        store.write_text("not-json", encoding="utf-8")
        self.assertFalse(inspect_svl(self.workspace)["stateValid"])
        store.write_bytes(b"x" * (256 * 1024 + 1))
        self.assertFalse(inspect_svl(self.workspace)["stateValid"])

    def test_private_api_and_offline_chat_command(self):
        api = Debian13Server(("127.0.0.1", 0), {
            "api_token": TOKEN, "model_chat_url": "", "model_api_key": "",
            "model_name": "", "workspace": str(self.workspace),
        })
        worker = threading.Thread(target=api.serve_forever, daemon=True)
        worker.start()
        try:
            for url, authorized in (
                ("/api/v1/svl/status", False),
                ("/api/v1/svl/status", True),
                ("/api/v1/status", True),
            ):
                conn = http.client.HTTPConnection(*api.server_address, timeout=4)
                try:
                    headers = {"Authorization": "Bearer " + TOKEN} if authorized else {}
                    conn.request("GET", url, headers=headers)
                    response = conn.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 200 if authorized else 401)
                    if authorized:
                        state = result["svl"] if url == "/api/v1/status" else result
                        self.assertTrue(state["sourceAvailable"])
                        self.assertFalse(state["operatingSystemBootVerified"])
                finally:
                    conn.close()
            conn = http.client.HTTPConnection(*api.server_address, timeout=4)
            try:
                conn.request(
                    "POST", "/v1/chat",
                    body=json.dumps({"profile": "aria", "message": "/svl"}),
                    headers={"Authorization": "Bearer " + TOKEN,
                             "Content-Type": "application/json"},
                )
                response = conn.getresponse()
                result = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(result["source"], "system_command")
                self.assertIsNone(result["model"])
                self.assertIn("SVL Python", result["content"])
                self.assertIn("nie uruchamia jądra Debian", result["content"])
            finally:
                conn.close()
        finally:
            api.shutdown()
            api.server_close()
            worker.join(timeout=3)


    def test_local_terminal_uses_svl_engine_without_ai_or_shell(self):
        state_path = self.workspace / ".svl_kernel_store.json"
        private_value = "confidential-operator-notes-must-not-be-printed"
        state_path.write_text(json.dumps({
            "config": {
                "sys_version": "5.6.0-QuantumWheel",
                "mode": "Local-Deterministic",
                "active_profiles": ["Alex", "Astyria"],
                "api_token": private_value,
            },
            "chat_history": [{"content": private_value}],
            "update_history": [{"description": private_value}],
        }), encoding="utf-8")
        before = state_path.read_bytes()
        api = Debian13Server(("127.0.0.1", 0), {
            "api_token": TOKEN, "model_chat_url": "", "model_api_key": "",
            "model_name": "", "workspace": str(self.workspace),
        })
        worker = threading.Thread(target=api.serve_forever, daemon=True)
        worker.start()
        try:
            for command in ("svl status", "svl report", "svl profiles"):
                for endpoint in ("/v1/terminal", "/api/v1/terminal"):
                    with self.subTest(command=command, endpoint=endpoint):
                        conn = http.client.HTTPConnection(*api.server_address, timeout=5)
                        try:
                            conn.request("POST", endpoint, body=json.dumps({"command": command}),
                                         headers={"Authorization": "Bearer " + TOKEN,
                                                  "Content-Type": "application/json"})
                            response = conn.getresponse()
                            result = json.loads(response.read())
                            self.assertEqual(response.status, 200)
                            self.assertEqual(result["exitCode"], 0)
                            output = result.get("stdout", result.get("output", ""))
                            self.assertIn("SVL", output) if command != "svl profiles" else self.assertIn("Alex", output)
                            self.assertNotIn(private_value, json.dumps(result))
                            self.assertNotIn(TOKEN, json.dumps(result))
                        finally:
                            conn.close()
            for command in ("svl", "svl bump", "svl report ; rm -rf .",
                            "svl status && whoami", "svl ../etc/passwd"):
                with self.subTest(blocked=command):
                    conn = http.client.HTTPConnection(*api.server_address, timeout=5)
                    try:
                        conn.request("POST", "/v1/terminal",
                                     body=json.dumps({"command": command}),
                                     headers={"Authorization": "Bearer " + TOKEN,
                                              "Content-Type": "application/json"})
                        response = conn.getresponse()
                        result = json.loads(response.read())
                        self.assertEqual(response.status, 400)
                        self.assertEqual(result["error"]["code"], "invalid_svl_command")
                    finally:
                        conn.close()
            self.assertEqual(state_path.read_bytes(), before)
        finally:
            api.shutdown()
            api.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
