import base64
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import Debian13Server, validate_provider_url


TOKEN = "test-token-with-at-least-thirty-two-characters"


class ProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.authorization = self.headers.get("Authorization")
        self.server.body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps({"choices": [{"message": {"content": "Real test-provider response."}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class Debian13ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "hello.txt").write_text("hello Debian 13\n", encoding="utf-8")
        self.server = Debian13Server(("127.0.0.1", 0), {
            "api_token": TOKEN,
            "model_chat_url": "",
            "model_api_key": "",
            "model_name": "",
            "workspace": str(self.workspace),
        })
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection(*self.server.server_address)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request_json(self, method, path, body=None, token=TOKEN):
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        self.connection.request(method, path, body=payload, headers=headers)
        response = self.connection.getresponse()
        self.last_retry_after = response.getheader("Retry-After")
        data = json.loads(response.read())
        return response.status, data

    def test_health_is_public_but_profiles_require_authentication(self):
        self.assertEqual(self.request_json("GET", "/healthz", token=None)[0], 200)
        status, _ = self.request_json("GET", "/v1/profiles", token=None)
        self.assertEqual(status, 401)
        status, data = self.request_json("GET", "/v1/profiles")
        self.assertEqual(status, 200)
        self.assertEqual([profile["id"] for profile in data["profiles"]], ["aria", "astyria", "aurelius", "alex"])
        self.assertTrue(all(profile["active"] is False for profile in data["profiles"]))

    def test_diagnostics_are_authenticated_and_do_not_disclose_secrets(self):
        self.assertEqual(self.request_json("GET", "/v1/diagnostics", token=None)[0], 401)
        self.assertEqual(self.request_json("GET", "/v1/diagnostics", token="invalid-token")[0], 401)
        self.server.config.update({
            "model_chat_url": "https://provider.example/v1/chat/completions",
            "model_api_key": "diagnostics-provider-secret",
            "model_name": "diagnostics-test-model",
        })
        status, data = self.request_json("GET", "/v1/diagnostics")
        self.assertEqual(status, 200)
        self.assertEqual(data["service"], "debian13-system")
        self.assertEqual(data["effectiveUid"], os.geteuid())
        self.assertTrue(data["workspaceWritable"])
        self.assertTrue(data["providerConfigured"])
        self.assertIsInstance(data["linuxSecurity"], dict)
        serialized = json.dumps(data)
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn("diagnostics-provider-secret", serialized)
        self.assertNotIn("diagnostics-test-model", serialized)

    def test_terminal_executes_allowlisted_argv_without_shell_or_secret_inheritance(self):
        status, data = self.request_json("POST", "/v1/terminal", {"command": "cat hello.txt"})
        self.assertEqual(status, 200)
        self.assertEqual(data["stdout"], "hello Debian 13\n")
        self.assertEqual(data["exitCode"], 0)

    def test_terminal_rejects_shells_unknown_commands_and_paths_outside_workspace(self):
        self.assertEqual(self.request_json("POST", "/v1/terminal", {"command": "sh -c id"})[0], 403)
        status, data = self.request_json("POST", "/v1/terminal", {"command": "cat ../../etc/passwd"})
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "path_outside_workspace")
        (self.workspace / "outside.txt").symlink_to("/etc/passwd")
        self.assertEqual(self.request_json("POST", "/v1/terminal", {"command": "cat outside.txt"})[0], 403)

    def test_terminal_output_is_bounded(self):
        (self.workspace / "large.txt").write_bytes(b"x" * (1024 * 1024 + 1))
        status, data = self.request_json("POST", "/v1/terminal", {"command": "cat large.txt"})
        self.assertEqual(status, 413)
        self.assertEqual(data["error"]["code"], "output_too_large")

    def test_chat_rejects_non_string_profile(self):
        status, data = self.request_json("POST", "/v1/chat", {"profile": [], "message": "test"})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "invalid_profile")

    def test_chat_fails_closed_without_a_model_provider(self):
        status, data = self.request_json("POST", "/v1/chat", {"profile": "aria", "message": "test"})
        self.assertEqual(status, 503)
        self.assertEqual(data["error"]["code"], "provider_not_configured")

    def test_chat_calls_configured_provider_with_selected_profile(self):
        provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        try:
            self.server.config.update({
                "model_chat_url": f"http://127.0.0.1:{provider.server_port}/v1/chat/completions",
                "model_api_key": "provider-secret",
                "model_name": "integration-test-model",
            })
            status, data = self.request_json("POST", "/v1/chat", {
                "profile": "aurelius",
                "message": "Review this test.",
            })
            self.assertEqual(status, 200)
            self.assertEqual(data["content"], "Real test-provider response.")
            self.assertEqual(data["profile"], "aurelius")
            self.assertEqual(provider.authorization, "Bearer " + self.server.config["model_api_key"])
            self.assertEqual(provider.body["messages"], [
                {"role": "user", "content": "Review this test."}
            ])
        finally:
            provider.shutdown()
            provider.server_close()
            provider_thread.join(timeout=2)

    def test_mobile_gui_chat_returns_real_provider_reply_in_sse_frames(self):
        provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        worker = threading.Thread(target=provider.serve_forever, daemon=True)
        worker.start()
        try:
            self.server.config.update({
                "model_chat_url": f"http://127.0.0.1:{provider.server_port}/v1/chat/completions",
                "model_api_key": "fixture-provider-key",
                "model_name": "fixture-model",
            })
            self.connection.request("POST", "/api/v1/chat/stream",
                                    body=json.dumps({"agent": "alex", "message": "Write tests"}),
                                    headers={"Authorization": "Bearer " + TOKEN,
                                             "Content-Type": "application/json"})
            reply = self.connection.getresponse()
            events = reply.read().decode("utf-8")
            self.assertEqual(reply.status, 200)
            self.assertTrue(reply.getheader("Content-Type").startswith("text/event-stream"))
            self.assertIn('event: delta\n', events)
            self.assertIn('"text": "Real test-provider response."', events)
            self.assertIn('event: done\n', events)
            self.assertIn('"session_persisted": true', events)
            self.assertEqual(provider.body["messages"][0]["content"], "Write tests")
        finally:
            provider.shutdown()
            provider.server_close()
            worker.join(timeout=3)

    def test_real_vision_payload_to_provider_and_invalid_input_rejected(self):
        provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        worker = threading.Thread(target=provider.serve_forever, daemon=True)
        worker.start()
        try:
            self.server.config.update({
                "model_chat_url": f"http://127.0.0.1:{provider.server_port}/v1/chat/completions",
                "model_api_key": "fixture-provider-key",
                "model_name": "fixture-text-model",
                "vision_model_name": "llama3.2-vision:11b",
                "ollama_vision_url": f"http://127.0.0.1:{provider.server_port}/v1/chat/completions",
                "ollama_vision_model": "llama3.2-vision:11b",
            })
            photo = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"test-image-bytes").decode()
            status, result = self.request_json("POST", "/v1/vision", {
                "profile": "aria", "message": "Describe this image.",
                "image": {"mime_type": "image/png", "base64": photo},
            })
            self.assertEqual(status, 200)
            self.assertEqual(result["model"], "llama3.2-vision:11b")
            self.assertEqual(provider.body["model"], "llama3.2-vision:11b")
            segments = provider.body["messages"][0]["content"]
            self.assertEqual(segments[0]["text"], "Describe this image.")
            self.assertTrue(segments[1]["image_url"].startswith("data:image/png;base64,"))
            self.assertEqual(result["source"], "ollama")
            status, data = self.request_json("POST", "/v1/vision", {
                "profile": "aria", "message": "Check",
                "image": {"mime_type": "image/png", "base64": "%%bad-base64"},
            })
            self.assertEqual(status, 400)
            self.assertEqual(data["error"]["code"], "invalid_image")
            status, _ = self.request_json("POST", "/v1/vision", {
                "profile": "aria", "message": "Check",
                "image": {"mime_type": "image/gif", "base64": photo},
            })
            self.assertEqual(status, 400)
        finally:
            provider.shutdown()
            provider.server_close()
            worker.join(timeout=3)

    def test_provider_url_rejects_untrusted_schemes_and_non_loopback_http(self):
        with self.assertRaises(ValueError):
            validate_provider_url("http://example.com/v1/chat/completions")
        with self.assertRaises(ValueError):
            validate_provider_url("******example.com/v1/chat/completions")

    def test_rate_limit_returns_429(self):
        self.server.config["rate_limit"] = 2
        self.assertEqual(self.request_json("GET", "/v1/profiles")[0], 200)
        self.assertEqual(self.request_json("GET", "/v1/profiles")[0], 200)
        status, _ = self.request_json("GET", "/v1/profiles")
        self.assertEqual(status, 429)
        self.assertTrue(self.last_retry_after.isdigit())


if __name__ == "__main__":
    unittest.main()
