"""Offline checks for read-only Debian13 release preflight."""
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[2] / "update_preflight.py"
spec = importlib.util.spec_from_file_location("debian13_update_preflight", SCRIPT)
assert spec is not None and spec.loader is not None
pre = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pre)

SHA = "1234567890abcdef1234567890abcdef12345678"
OTHER_SHA = "abcdef1234567890abcdef1234567890abcdef12"


def ci_run(sha=SHA, event="push", result="success", branch="main"):
    return {
        "name": "Debian 13 validation",
        "head_sha": sha, "head_branch": branch, "event": event,
        "status": "completed", "conclusion": result,
    }


class UpdatePreflightTests(unittest.TestCase):
    def test_verified_main_with_successful_push_ci_allows_review_only(self):
        value = pre.evaluate("main", SHA, True, SHA, {"workflow_runs": [ci_run()]})
        self.assertTrue(value["eligibleForManualReleaseReview"])
        self.assertFalse(value["deploymentPerformed"])
        self.assertFalse(value["productionReadinessVerified"])
        self.assertTrue(all(value["checks"].values()))

    def test_pull_request_ci_never_unlocks_release(self):
        value = pre.evaluate("main", SHA, True, SHA, {"workflow_runs": [
            ci_run(event="pull_request"),
        ]})
        self.assertFalse(value["eligibleForManualReleaseReview"])
        self.assertFalse(value["checks"]["ci_success_on_exact_main_commit"])

    def test_dirty_branch_or_stale_sha_fails_closed(self):
        for branch, local_sha, clean in (
            ("feature/dialog-triady-vision-memory", SHA, True),
            ("main", OTHER_SHA, True),
            ("main", SHA, False),
            ("", SHA, True),
            ("main", "invalid-sha", True),
        ):
            with self.subTest(branch=branch, sha=local_sha, clean=clean):
                value = pre.evaluate(branch, local_sha, clean, SHA, {
                    "workflow_runs": [ci_run()],
                })
                self.assertFalse(value["eligibleForManualReleaseReview"])

    def test_unverified_ci_cannot_mark_ready(self):
        for data in (
            None, {}, {"workflow_runs": "invalid"},
            {"workflow_runs": [ci_run(sha=OTHER_SHA)]},
            {"workflow_runs": [ci_run(branch="feature")]},
            {"workflow_runs": [ci_run(result="cancelled")]},
            {"workflow_runs": [ci_run(result="failure")]},
            {"workflow_runs": [dict(ci_run(), status="in_progress")]},
        ):
            with self.subTest(data=data):
                value = pre.evaluate("main", SHA, True, SHA, data)
                self.assertFalse(value["eligibleForManualReleaseReview"])

    def test_inspect_uses_exact_remote_sha_and_read_only_commands(self):
        calls = []
        def git(*args):
            calls.append(("git", args))
            return {
                ("branch", "--show-current"): "main",
                ("rev-parse", "HEAD"): SHA,
                ("status", "--porcelain=v1", "--untracked-files=normal"): "",
            }[args]
        def fetch(endpoint):
            calls.append(("github", endpoint))
            if endpoint == "commits/main":
                return {"sha": SHA}
            if endpoint == f"actions/runs?head_sha={SHA}&branch=main&per_page=30":
                return {"workflow_runs": [ci_run()]}
            raise AssertionError("Unexpected network destination")
        with patch.object(pre, "read_git", side_effect=git), patch.object(
            pre, "read_github", side_effect=fetch
        ):
            value = pre.inspect()
        self.assertTrue(value["eligibleForManualReleaseReview"])
        self.assertEqual(len(calls), 5)
        self.assertFalse(any(
            any(cmd in str(arg) for cmd in ("pull", "checkout", "merge", "reset"))
            for kind, arg in calls if kind == "git"
        ))

    def test_refuses_non_github_endpoints_and_traversal(self):
        for endpoint in ("https://evil.test", "../secrets", "commits/feature",
                         "actions/runs?head_sha=bad&branch=main&per_page=30"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(pre.PreflightError):
                    pre.read_github(endpoint)

    def test_git_failure_redacts_stderr(self):
        secret = "secret-access-token-in-git-error"
        result = subprocess.CompletedProcess(["git"], 1, "", secret)
        with patch.object(pre.subprocess, "run", return_value=result):
            with self.assertRaises(pre.PreflightError) as error:
                pre.read_git("rev-parse", "HEAD")
        self.assertNotIn(secret, str(error.exception))

    def test_remote_invalid_sha_blocks_without_ci_lookup(self):
        with patch.object(pre, "read_git", side_effect=["main", SHA, ""]), patch.object(
            pre, "read_github", return_value={"sha": "broken"}
        ) as github:
            with self.assertRaises(pre.PreflightError) as exc:
                pre.inspect()
        self.assertEqual(str(exc.exception), "remote_main_sha_unverified")
        github.assert_called_once_with("commits/main")

    def test_cli_failure_is_nonzero_and_secret_free(self):
        secret = "private-upstream-token-must-not-leak"
        with patch.object(pre, "inspect", side_effect=pre.PreflightError("github_unavailable_or_denied")):
            with patch.object(sys, "argv", ["update_preflight.py", "--json"]), patch(
                "sys.stdout", new_callable=io.StringIO
            ) as output:
                code = pre.main()
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["deploymentPerformed"])
        self.assertFalse(result["eligibleForManualReleaseReview"])
        self.assertNotIn(secret, output.getvalue())


if __name__ == "__main__":
    unittest.main()
