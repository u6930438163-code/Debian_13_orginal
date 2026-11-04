"""Authenticated, voluntary AI-to-AI messaging on the original Debian 13 API.

These tests use actual loopback HTTP and SQLite, not a mock agent inference.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Debian13Server

OPERATOR_TOKEN = "test-operator-token-with-minimum-32-characters"
ALEX_TOKEN = "test-alex-private-token-with-minimum-32-characters"
ARIA_TOKEN = "test-aria-private-token-with-minimum-32-characters"


class AgentDialogIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "DEBIAN13_DIALOG_ALEX_TOKEN": ALEX_TOKEN,
            "DEBIAN13_DIALOG_ARIA_TOKEN": ARIA_TOKEN,
        })
        self.env.start()
        self.server = Debian13Server(("127.0.0.1", 0), {
            "api_token": OPERATOR_TOKEN, "workspace": self.workspace.name,
            "model_chat_url": "", "model_api_key": "", "model_name": "",
        })
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.env.stop()
        self.workspace.cleanup()

    def request(self, method, path, token=None, payload=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        request_headers = dict(headers or {})
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        body = None
        if payload is not None:
            body = json.dumps(payload)
            request_headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=request_headers)
        res = conn.getresponse()
        status = res.status
        data = json.loads(res.read())
        conn.close()
        return status, data

    def test_operator_to_all_and_agent_to_agent_no_auto_reply(self):
        status, first = self.request("POST", "/api/v1/agent-dialog/messages",
                                     OPERATOR_TOKEN, {"to": "all", "content": "Witajcie!"})
        self.assertEqual(status, 201)
        self.assertEqual(first["message"]["sender"], "operator")
        self.assertFalse(first["automaticReplyScheduled"])
        self.assertFalse(first["agentInferenceVerified"])

        status, reply = self.request("POST", "/api/v1/agent-dialog/messages",
                                     ALEX_TOKEN, {"to": "aria", "content": "Możesz sprawdzić plan?"})
        self.assertEqual(status, 201)
        self.assertEqual(reply["message"]["sender"], "alex")
        status, aria = self.request("GET", "/api/v1/agent-dialog/messages", ARIA_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual([m["sender"] for m in aria["messages"]], ["operator", "alex"])
        self.assertFalse(aria["agentInferenceVerified"])
        status, alex = self.request("GET", "/api/v1/agent-dialog/messages", ALEX_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(len(alex["messages"]), 2)
        status, op = self.request("GET", "/api/v1/agent-dialog/messages", OPERATOR_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(len(op["messages"]), 2)

        status, aria_new = self.request("GET",
            "/api/v1/agent-dialog/messages?after=" + str(reply["message"]["id"]), ARIA_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(aria_new["messages"], [])

        # A second database connection reads the same persisted events.
        from chat_memory import ChatMemory
        persisted = ChatMemory(Path(self.workspace.name)).get_agent_dialog("operator")
        self.assertEqual(len(persisted), 2)

    def test_messages_do_not_spoof_sender_and_private_inboxes_are_isolated(self):
        self.assertEqual(self.request("POST", "/api/v1/agent-dialog/messages", OPERATOR_TOKEN,
                                      {"to": "alex", "content": "Private for Alex"})[0], 201)
        self.assertEqual(self.request("POST", "/api/v1/agent-dialog/messages", ALEX_TOKEN,
                                      {"to": "operator", "content": "Only the owner"})[0], 201)
        self.assertEqual(self.request("GET", "/api/v1/agent-dialog/messages", ARIA_TOKEN)[1]["messages"], [])
        self.assertEqual(len(self.request("GET", "/api/v1/agent-dialog/messages", OPERATOR_TOKEN)[1]["messages"]), 2)
        status, data = self.request("POST", "/api/v1/agent-dialog/messages", ALEX_TOKEN,
                                    {"to": "all", "content": "pretend", "sender": "aria"})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "invalid_dialog_message")

    def test_authentication_and_invalid_input_fail_closed(self):
        for token in (None, "incorrect-token"):
            self.assertEqual(self.request("GET", "/api/v1/agent-dialog/messages", token)[0], 401)
            self.assertEqual(self.request("POST", "/api/v1/agent-dialog/messages", token,
                                          {"to": "all", "content": "X"})[0], 401)
        for payload in (
            {"to": "unknown", "content": "text"},
            {"to": "aria", "content": ""},
            {"to": "all", "content": "x" * 4001},
        ):
            self.assertEqual(self.request("POST", "/api/v1/agent-dialog/messages", ALEX_TOKEN, payload)[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/agent-dialog/messages?after=-2", OPERATOR_TOKEN)[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/agent-dialog/messages?foo=1", OPERATOR_TOKEN)[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/agent-dialog/messages", OPERATOR_TOKEN)[1]["messages"], [])

    def test_other_model_engine_can_use_the_same_message_bus(self):
        # No reliance on Ollama: the message API is deliberately model-neutral.
        from agent_dialog_client import call
        url = "http://127.0.0.1:" + str(self.server.server_port)
        self.server.config.update({
            "model_chat_url": "https://provider.example/v1/chat/completions",
            "model_api_key": "not-a-real-key", "model_name": "different-engine",
        })
        sent = call("alex", "POST", to="aria", content="Czytamy ten sam kanał.",
                    url=url)
        self.assertTrue(sent["accepted"])
        self.assertFalse(sent["automaticReplyScheduled"])
        received = call("aria", "GET", after=0, url=url)
        self.assertEqual(received["messages"][0]["sender"], "alex")
        self.assertEqual(received["messages"][0]["content"], "Czytamy ten sam kanał.")
        self.assertFalse(received["agentInferenceVerified"])

    def test_optional_client_rejects_remote_host_and_missing_private_token(self):
        from agent_dialog_client import trusted_local_address, call, DialogClientError
        for address in (
            "https://api.openai.com/v1/chat/completions",
            "http://example.org:8765",
            "http://127.0.0.1:8765/path",
            "http://user:secret@localhost:8765",
            "http://127.0.0.1:8765/?token=secret",
        ):
            with self.subTest(address=address), self.assertRaises(DialogClientError):
                trusted_local_address(address)
        with patch.dict(os.environ, {"DEBIAN13_DIALOG_ALEX_TOKEN": ""}):
            with self.assertRaises(DialogClientError):
                call("alex", "GET", url="http://127.0.0.1:" + str(self.server.server_port))



if __name__ == "__main__":
    unittest.main()
