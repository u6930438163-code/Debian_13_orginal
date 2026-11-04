"""Core Debian13 readiness works without requiring any original AI agent."""
import contextlib
import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

FILE = Path(__file__).resolve().parents[1] / "readiness.py"
spec = importlib.util.spec_from_file_location("debian13_readiness", FILE)
assert spec and spec.loader
ready = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ready)

TOKEN = "readiness-test-token-with-more-than-32-characters"


def responder(url, path, token=None):
    if path == "/healthz":
        return 200, {"status": "ok"}
    if path == "/v1/diagnostics" and token is None:
        return 401, {"error": {"code": "unauthorized"}}
    if path == "/v1/diagnostics" and token == TOKEN:
        return 200, {"service": "debian13-system"}
    if path == "/v1/profiles" and token == TOKEN:
        return 200, {"providerConfigured": False, "profiles": []}
    raise AssertionError("Unexpected network destination or token")


class ReadinessTests(unittest.TestCase):
    def run_check(self, args=(), responder_impl=responder, token=TOKEN, release=None):
        with patch.dict(os.environ, {"DEBIAN13_API_TOKEN": token,
                                     "DEBIAN13_SYSTEM_API_URL": "http://127.0.0.1:8080"}), \
             patch.object(ready, "request_json", side_effect=responder_impl), \
             patch.object(ready, "os_release_values", return_value=release or {
                 "ID": "debian", "VERSION_ID": "13",
             }), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = ready.main(list(args))
        return code, output.getvalue()

    def test_core_is_ready_without_model_or_agents(self):
        code, text = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("Core API ready independently", text)
        self.assertIn("Inference: UNVERIFIED", text)
        self.assertIn("NOT CONFIGURED", text)

    def test_require_provider_is_explicit_opt_in(self):
        code, text = self.run_check(["--require-provider"])
        self.assertEqual(code, 1)
        self.assertIn("Optional provider required", text)

    def test_missing_auth_or_invalid_os_is_not_ready(self):
        code, _ = self.run_check(token="not-a-valid-token")
        self.assertEqual(code, 1)
        code, _ = self.run_check(release={"ID": "ubuntu", "VERSION_ID": "24.04"})
        self.assertEqual(code, 1)

    def test_core_never_claims_success_if_api_does_not_reject_anonymous(self):
        def open_api(url, path, token=None):
            if path == "/v1/diagnostics" and token is None:
                return 200, {"service": "debian13-system"}
            return responder(url, path, token)
        code, _ = self.run_check(responder_impl=open_api)
        self.assertEqual(code, 1)

    def test_readiness_does_not_send_model_requests(self):
        seen = []
        def audit(url, path, token=None):
            seen.append(path)
            return responder(url, path, token)
        code, _ = self.run_check(responder_impl=audit)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [
            "/healthz", "/v1/diagnostics", "/v1/diagnostics", "/v1/profiles",
        ])


if __name__ == "__main__":
    unittest.main()
