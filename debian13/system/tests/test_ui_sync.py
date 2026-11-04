"""End-to-end tests of durable chat-command -> API -> UI state synchronization.

No external AI model is invoked: slash-command execution is a system feature.
"""
from __future__ import annotations

import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Debian13Server
from ui_sync import SyncStore, SyncValidationError, parse_title_command

TOKEN = "test-sync-token-longer-than-32-characters"


class SyncStoreTests(unittest.TestCase):
    def test_persistence_versioning_and_event_history(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = SyncStore(workspace)
            self.assertEqual(first.snapshot()["revision"], 0)
            state = first.set_title("Moje Centrum AI")
            self.assertEqual(state["title"], "Moje Centrum AI")
            self.assertEqual(state["revision"], 1)
            self.assertEqual(state["events"][0]["source"], "operator_command")
            again = SyncStore(workspace)
            self.assertEqual(again.snapshot()["title"], "Moje Centrum AI")
            self.assertEqual(again.set_title("Moje Centrum AI")["revision"], 1)
            self.assertEqual(again.set_title("Panel globalny")["revision"], 2)
            self.assertEqual(stat.S_IMODE(again.path.stat().st_mode), 0o600)

    def test_only_explicit_command_accepted(self):
        self.assertIsNone(parse_title_command("Alex, zmień wygląd panelu"))
        self.assertEqual(parse_title_command("/panel tytuł Widok AI"), "Widok AI")
        self.assertEqual(parse_title_command("/ui title Unified view"), "Unified view")
        for command in ("/panel", "/panel tytuł", "/ui title "):
            with self.assertRaises(SyncValidationError):
                parse_title_command(command)
        with tempfile.TemporaryDirectory() as directory:
            s = SyncStore(Path(directory))
            with self.assertRaises(SyncValidationError):
                s.set_title("x" * 81)
            with self.assertRaises(SyncValidationError):
                s.set_title("Bad\nTitle")
            self.assertEqual(s.snapshot()["revision"], 0)


class SyncApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Debian13Server(("127.0.0.1", 0), {
            "api_token": TOKEN, "model_chat_url": "", "model_api_key": "",
            "model_name": "", "workspace": self.temp.name,
        })
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, payload=None, authenticated=True):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {}
        if authenticated:
            headers["Authorization"] = "Bearer " + TOKEN
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload)
        client.request(method, path, body=body, headers=headers)
        response = client.getresponse()
        status = response.status
        value = response.read()
        client.close()
        return status, value

    def test_sync_requires_auth_and_real_command_changes_a_second_client(self):
        self.assertEqual(self.request("GET", "/api/v1/sync/state",
                                      authenticated=False)[0], 401)
        status, raw = self.request("POST", "/api/v1/chat/stream", {
            "agent": "alex", "message": "/panel tytuł Globalny widok",
        })
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        self.assertIn("event: ui_update", text)
        self.assertIn('source": "system_command"', text)
        self.assertIn("event: done", text)
        self.assertNotIn("model_provider", text)
        status, raw = self.request("GET", "/api/v1/sync/state")
        self.assertEqual(status, 200)
        state = json.loads(raw)
        self.assertEqual(state["title"], "Globalny widok")
        self.assertEqual(state["revision"], 1)
        self.assertFalse(state["realAgentActionVerified"])
        self.assertEqual(state["events"][0]["source"], "operator_command")

        # A restarted process sees the same revision in persisted SQLite.
        reopened = SyncStore(Path(self.temp.name))
        self.assertEqual(reopened.snapshot()["title"], "Globalny widok")
        self.assertEqual(reopened.snapshot()["revision"], 1)

    def test_regular_chat_and_invalid_commands_do_not_change_interface(self):
        status, _ = self.request("POST", "/api/v1/chat/stream", {
            "agent": "aria", "message": "Zmień tytuł panelu na fikcyjny",
        })
        self.assertEqual(status, 503)  # No configured inference.
        status, raw = self.request("POST", "/api/v1/chat/stream", {
            "agent": "aria", "message": "/panel tytuł " + ("x" * 81),
        })
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"]["code"], "invalid_ui_command")
        self.assertEqual(json.loads(
            self.request("GET", "/api/v1/sync/state")[1])["revision"], 0)

    def test_operator_slash_command_requires_token(self):
        status, _ = self.request("POST", "/api/v1/chat/stream", {
            "agent": "alex", "message": "/panel tytuł Unauthorized",
        }, authenticated=False)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(
            self.request("GET", "/api/v1/sync/state")[1])["revision"], 0)


if __name__ == "__main__":
    unittest.main()
