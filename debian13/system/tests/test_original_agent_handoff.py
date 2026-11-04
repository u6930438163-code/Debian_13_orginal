"""Verified API contract with mock original AI Studio worker; no paid inference.

An HTTP mock only proves protocol handling. It is not evidence that the
owner's real Alex, Aria, Astyria or Aurelius ran a model.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Debian13Server
from original_agent_handoff import OriginalAgentError, capabilities, submit, status

TOKEN = "private-operator-token-for-tests-only-123456789"
SOURCE_TOKEN = "private-original-agent-token-for-tests-only"


class OriginalAgentMock(BaseHTTPRequestHandler):
    calls = []
    valid_target = True
    def log_message(self, *args):
        pass
    def send(self, status_code, value):
        data = json.dumps(value).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        type(self).calls.append({"path": self.path,
                                "token": self.headers.get("Authorization")})
        if self.path == "/api/agent-tasks/capabilities":
            self.send(200, {
                "sourceRepository": "u6930438163-code/AI_Studio_BastillaOS",
                "targetRepository": ("mmitek515-netizen/Debian_13" if type(self).valid_target
                                    else "u6930438163-code/Debian_13_Trixie"),
                "protocolVersion": 1, "agents": ["Alex", "Aria", "Astyria", "Aurelius"],
                "agent": "Alex", "capabilities": ["debian13_review_task_v1",
                                                 "debian13_agent_message_v1"],
                "assignedRoles": False, "customInstructions": False,
                "workerExecutionVerified": False, "mode": "model_review_only",
            })
        elif self.path.startswith("/api/agent-tasks/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            self.send(200, {
                "id": job_id, "agent": "Aria", "status": "completed",
                "workerExecutionVerified": True,
                "provider": "ollama", "model": "test-model",
                "result": "Factual model review: test only.",
                "pullRequestVerified": False,
            })
        else:
            self.send(404, {"error": "not_found"})
    def do_POST(self):
        if self.path != "/api/agent-tasks/submit":
            self.send(404, {"error": "not_found"})
            return
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        payload = json.loads(raw)
        type(self).calls.append({
            "path": self.path, "body": payload,
            "token": self.headers.get("Authorization"),
            "key": self.headers.get("Idempotency-Key"),
        })
        self.send(202, {
            "accepted": True, "agent": payload["agent"],
            "capability": "debian13_review_task_v1",
            "taskId": "123e4567-e89b-42d3-a456-426614174000",
            "status": "queued", "workerExecutionVerified": False,
        })


class OriginalReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = ThreadingHTTPServer(("127.0.0.1", 0), OriginalAgentMock)
        cls.source_thread = threading.Thread(target=cls.source.serve_forever, daemon=True)
        cls.source_thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.source.shutdown()
        cls.source.server_close()
        cls.source_thread.join(timeout=3)
    def setUp(self):
        OriginalAgentMock.calls = []
        OriginalAgentMock.valid_target = True
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "ALEX_EXISTING_API_URL": "http://127.0.0.1:"
                                   + str(self.source.server_port) + "/api",
            "ORIGINAL_AGENTS_API_TOKEN": SOURCE_TOKEN,
            "ORIGINAL_AGENTS_ALLOW_REVIEW": "1",
        })
        self.env.start()
        self.api = Debian13Server(("127.0.0.1", 0), {
            "api_token": TOKEN, "model_chat_url": "", "model_api_key": "",
            "model_name": "", "workspace": self.tmp.name,
        })
        self.thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.thread.start()
    def tearDown(self):
        self.api.shutdown()
        self.api.server_close()
        self.thread.join(timeout=3)
        self.env.stop()
        self.tmp.cleanup()
    def request(self, method, path, body=None, auth=True):
        connection = http.client.HTTPConnection(*self.api.server_address, timeout=10)
        headers = {"Authorization": "Bearer " + TOKEN} if auth else {}
        if body is not None: headers["Content-Type"] = "application/json"
        connection.request(method, path,
                           body=json.dumps(body) if body is not None else None,
                           headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data
    def task(self, agent):
        status_code, r = self.request("POST", "/api/v1/tasks", {
            "agent": agent, "kind": "development", "title": "Owner task",
            "description": "Review specifically requested code changes.",
        })
        self.assertEqual(status_code, 201)
        return r["task"]["id"]

    def test_capability_checks_exact_target_and_explicit_opt_in(self):
        self.assertTrue(capabilities()["supported"])
        self.assertFalse(capabilities()["agentRuntimeVerified"])
        OriginalAgentMock.valid_target = False
        self.assertFalse(capabilities()["supported"])
        self.assertEqual(capabilities()["reason"], "protocol_mismatch")
        OriginalAgentMock.calls = []
        with patch.dict(os.environ, {"ORIGINAL_AGENTS_ALLOW_REVIEW": "0"}):
            self.assertFalse(capabilities()["supported"])
            self.assertEqual(OriginalAgentMock.calls, [])
        with patch.dict(os.environ, {"ORIGINAL_AGENTS_API_TOKEN": "bad"}):
            with self.assertRaises(OriginalAgentError):
                capabilities()

    def test_handoff_all_four_and_remote_status_separate_from_pr(self):
        for agent, source_name in (("alex", "Alex"), ("aria", "Aria"),
                                    ("astyria", "Astyria"), ("aurelius", "Aurelius")):
            with self.subTest(agent=agent):
                ident = self.task(agent)
                code, result = self.request("POST", f"/api/v1/tasks/{ident}/handoff")
                self.assertEqual(code, 202)
                self.assertFalse(result["agentExecutionVerified"])
                record = result["originalSubmission"]
                self.assertEqual(record["agent"], agent)
                self.assertEqual(record["status"], "accepted_by_remote_api")
                posts = [x for x in OriginalAgentMock.calls
                         if x["path"] == "/api/agent-tasks/submit"]
                posted = posts[-1]
                self.assertEqual(posted["body"]["agent"], source_name)
                self.assertEqual(posted["body"]["task"]["targetRepository"],
                                 "mmitek515-netizen/Debian_13")
                self.assertEqual(posted["key"], ident)
                self.assertFalse(posted["body"]["autoMerge"])
                self.assertFalse(posted["body"]["purchaseAuthorized"])
                self.assertEqual(posted["token"], "Bearer " + SOURCE_TOKEN)
                again, result = self.request("POST", f"/api/v1/tasks/{ident}/handoff")
                self.assertEqual(again, 200)
                self.assertEqual(len([x for x in OriginalAgentMock.calls
                                      if x["path"] == "/api/agent-tasks/submit"]),
                                 len(posts))
                self.assertEqual(self.request("POST", f"/api/v1/tasks/{ident}/cancel")[0],
                                 409)
                detail = self.request("GET", f"/api/v1/tasks/{ident}")[1]
                self.assertIsNotNone(detail["originalSubmission"])
        self.assertEqual(len([x for x in OriginalAgentMock.calls
                              if x["path"] == "/api/agent-tasks/submit"]), 4)

    def test_genuine_model_result_is_not_a_commit_and_auth_is_required(self):
        task_id = self.task("aria")
        self.request("POST", f"/api/v1/tasks/{task_id}/handoff")
        code, data = self.request("GET", f"/api/v1/tasks/{task_id}/original-status")
        self.assertEqual(code, 200)
        self.assertTrue(data["originalJob"]["agentExecutionVerified"])
        self.assertFalse(data["originalJob"]["pullRequestVerified"])
        self.assertEqual(data["originalJob"]["provider"], "ollama")
        self.assertIn("Factual model review", data["originalJob"]["result"])
        code, _ = self.request("GET", f"/api/v1/tasks/{task_id}/original-status",
                               auth=False)
        self.assertEqual(code, 401)
        code, _ = self.request("POST", f"/api/v1/tasks/{task_id}/handoff",
                               auth=False)
        self.assertEqual(code, 401)

    def test_does_not_contact_source_without_opt_in(self):
        task_id = self.task("alex")
        OriginalAgentMock.calls = []
        with patch.dict(os.environ, {"ORIGINAL_AGENTS_ALLOW_REVIEW": "0"}):
            code, data = self.request("POST", f"/api/v1/tasks/{task_id}/handoff")
        self.assertEqual(code, 409)
        self.assertEqual(data["error"]["code"], "unsupported_protocol")
        self.assertEqual(OriginalAgentMock.calls, [])


if __name__ == "__main__":
    unittest.main()
