"""Independent AI Center lifecycle and security regression tests.

All inference is mocked; no real model, remote account, shell or root access.
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
from work_store import WorkStore
from server import Debian13Server
import ai_center_worker

TOKEN = "ai-center-test-token-with-at-least-32-characters"


class WorkerTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkStore(Path(self.temp.name))
        self.task = self.store.create_task(
            "development", "alex", "Design a test",
            "Propose safe Python unit tests for Debian 13.",
        )
        self.config = {
            "model_chat_url": "http://127.0.0.1:11434/v1/chat/completions",
            "model_api_key": "ollama-local-no-remote-secret",
            "model_name": "test-local-ollama-model",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_worker_never_executes_unapproved_jobs(self):
        with patch.object(ai_center_worker, "complete_chat") as mocked:
            self.assertFalse(ai_center_worker.run_once(self.store, self.config))
            mocked.assert_not_called()
        self.assertEqual(self.store.get_task(self.task["id"])["execution"], "not_started")
        self.assertIsNone(self.store.result_for_task(self.task["id"]))

    def test_approved_job_runs_local_model_and_saves_real_report(self):
        approved = self.store.approve_local_analysis(self.task["id"])
        self.assertEqual(approved["execution"], "approved_local_analysis")
        with patch.object(ai_center_worker, "complete_chat",
                          return_value=("Reviewed report based on model.", "test-local-ollama-model")) as provider:
            self.assertTrue(ai_center_worker.run_once(self.store, self.config))
        self.assertEqual(provider.call_count, 1)
        self.assertIn("Do not claim to have executed", provider.call_args[0][1])
        task = self.store.get_task(self.task["id"])
        self.assertEqual(task["execution"], "completed_local_analysis")
        self.assertEqual(task["requires_approval"], 1)
        report = self.store.result_for_task(self.task["id"])
        self.assertEqual(report["content"], "Reviewed report based on model.")
        self.assertFalse(ai_center_worker.run_once(self.store, self.config))

    def test_failed_inference_does_not_fabricate_success(self):
        self.store.approve_local_analysis(self.task["id"])
        with patch.object(ai_center_worker, "complete_chat", side_effect=OSError("offline")):
            self.assertTrue(ai_center_worker.run_once(self.store, self.config))
        self.assertEqual(self.store.get_task(self.task["id"])["execution"], "failed_local_analysis")
        self.assertIsNone(self.store.result_for_task(self.task["id"]))

    def test_restart_marks_in_flight_work_as_interrupted(self):
        self.store.approve_local_analysis(self.task["id"])
        self.assertEqual(self.store.claim_local_analysis()["id"], self.task["id"])
        self.assertEqual(self.store.recover_interrupted_analysis(), 1)
        task = self.store.get_task(self.task["id"])
        self.assertEqual(task["execution"], "interrupted_local_analysis")
        self.assertEqual(task["requires_approval"], 1)
        self.assertEqual(self.store.recover_interrupted_analysis(), 0)

    def test_cancelled_task_discards_late_model_answer(self):
        self.store.approve_local_analysis(self.task["id"])
        self.store.claim_local_analysis()
        self.store.cancel_task(self.task["id"])
        self.assertFalse(self.store.finish_local_analysis(
            self.task["id"], model="x", content="Too late"))
        self.assertIsNone(self.store.result_for_task(self.task["id"]))

    def test_only_private_ollama_endpoint_allowed(self):
        for url in ("https://api.external.com/v1/chat/completions",
                    "http://evil.example:11434/v1/chat/completions",
                    "http://127.0.0.1:9999/v1/chat/completions"):
            with patch.dict("os.environ", {"OLLAMA_CHAT_URL": url}):
                with self.assertRaises(ValueError):
                    ai_center_worker.local_ollama_url()
        with patch.dict("os.environ", {
            "OLLAMA_CHAT_URL": "http://127.0.0.1:11434/v1/chat/completions"}):
            self.assertEqual(ai_center_worker.local_ollama_url(),
                             "http://127.0.0.1:11434/v1/chat/completions")


class WorkerApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Debian13Server(("127.0.0.1", 0), {
            "api_token": TOKEN, "workspace": self.temp.name,
            "model_chat_url": "", "model_api_key": "", "model_name": "",
        })
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=4)
        self.temp.cleanup()

    def request(self, method, path, payload=None, token=TOKEN):
        con = http.client.HTTPConnection(*self.server.server_address, timeout=4)
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = json.dumps(payload) if payload is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            con.request(method, path, body=body, headers=headers)
            reply = con.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            con.close()

    def test_approval_requires_token_and_does_not_claim_execution(self):
        task = self.server.work_store.create_task(
            "audit", "aurelius", "Review design", "Find security risks.")
        path = f"/api/v1/tasks/{task['id']}/approve-analysis"
        self.assertEqual(self.request("POST", path, token=None)[0], 401)
        self.assertEqual(self.request("POST", path, payload={"root": True})[0], 400)
        status, data = self.request("POST", path)
        self.assertEqual(status, 202)
        self.assertFalse(data["executionStarted"])
        self.assertFalse(data["localWorkerHeartbeatVerified"])
        self.assertEqual(data["task"]["execution"], "approved_local_analysis")
        self.assertEqual(self.request("POST", path)[0], 409)
        self.assertEqual(self.request("GET", f"/api/v1/tasks/{task['id']}")[1]["localAnalysis"], None)
        self.assertFalse(self.request("GET", "/api/v1/ai-center/status")[1]["workerHeartbeatVerified"])

    def test_model_heartbeat_does_not_prove_inference_or_original_agent_connection(self):
        self.server.work_store.heartbeat_local_worker("test-model")
        status, data = self.request("GET", "/api/v1/ai-center/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["workerHeartbeatVerified"])
        self.assertFalse(data["executionVerified"])
        self.assertFalse(data["originalAgentsConnected"])
        self.assertFalse(data["chatGPTWorkRequired"])


if __name__ == "__main__":
    unittest.main()
