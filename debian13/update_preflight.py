#!/usr/bin/env python3
"""Read-only update readiness gate for Debian13_orginal.

It NEVER pulls, checks out, executes code from GitHub, merges, deploys, restarts
services, or prints credentials. An approved release still requires a separately
authorized manual deployment and a verification/rollback plan.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPOSITORY = "mmitek515-netizen/Debian_13"
BASE = "https://api.github.com/repos/" + REPOSITORY
ROOT = Path(__file__).resolve().parents[1]
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
MAX_RESPONSE = 512 * 1024


class PreflightError(Exception):
    """Neutral failure text without local paths, remote content or secrets."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True,
            text=True, timeout=6, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PreflightError("local_git_unavailable") from None
    if result.returncode != 0:
        raise PreflightError("local_git_error") from None
    return result.stdout.strip()


def read_github(endpoint: str) -> Any:
    # Caller controls only one verified 40-character SHA, never a hostname.
    if endpoint != "commits/main" and not re.fullmatch(
        r"actions/runs\?head_sha=[0-9a-f]{40}&branch=main&per_page=30",
        endpoint,
    ):
        raise PreflightError("unsupported_endpoint")
    req = urllib.request.Request(
        BASE + "/" + endpoint,
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Debian13-Manual-Update-Preflight/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    )
    try:
        with opener.open(req, timeout=5) as response:
            if response.status != 200:
                raise PreflightError("github_status_unexpected")
            data = response.read(MAX_RESPONSE + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        raise PreflightError("github_unavailable_or_denied") from None
    if len(data) > MAX_RESPONSE:
        raise PreflightError("github_response_too_large")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise PreflightError("github_invalid_json") from None


def evaluate(local_branch: str, local_sha: str, git_clean: bool,
             remote_sha: str, runs: object) -> dict:
    """GitHub successful PR tests alone are never valid for releasing main."""
    valid_local_sha = bool(SHA_PATTERN.fullmatch(local_sha))
    valid_remote_sha = bool(SHA_PATTERN.fullmatch(remote_sha))
    successful_main_ci = False
    if isinstance(runs, dict) and isinstance(runs.get("workflow_runs"), list):
        successful_main_ci = any(
            isinstance(run, dict)
            and run.get("name") == "Debian 13 validation"
            and run.get("head_sha") == remote_sha
            and run.get("head_branch") == "main"
            and run.get("event") in ("push", "workflow_dispatch")
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            for run in runs["workflow_runs"]
        )
    checks = {
        "local_branch_main": local_branch == "main",
        "local_commit_valid": valid_local_sha,
        "workspace_clean": git_clean is True,
        "remote_main_valid": valid_remote_sha,
        "local_matches_remote_main": valid_local_sha and valid_remote_sha and local_sha == remote_sha,
        "ci_success_on_exact_main_commit": valid_remote_sha and successful_main_ci,
    }
    return {
        "repository": REPOSITORY,
        "mode": "read_only_manual_update_preflight",
        "localCommit": local_sha if valid_local_sha else None,
        "remoteMainCommit": remote_sha if valid_remote_sha else None,
        "checks": checks,
        "eligibleForManualReleaseReview": all(checks.values()),
        "deploymentPerformed": False,
        "productionReadinessVerified": False,
        "agentExecutionVerified": False,
        "explanation": "Passing checks permit manual review only; they do not deploy or boot Debian 13.",
    }


def inspect() -> dict:
    # Never fetch or modify the local Git tree. Always fail closed.
    local_branch = read_git("branch", "--show-current")
    local_sha = read_git("rev-parse", "HEAD")
    local_clean = not bool(read_git("status", "--porcelain=v1", "--untracked-files=normal"))
    main = read_github("commits/main")
    remote_sha = main.get("sha", "") if isinstance(main, dict) else ""
    if not SHA_PATTERN.fullmatch(remote_sha):
        raise PreflightError("remote_main_sha_unverified")
    runs = read_github(
        f"actions/runs?head_sha={remote_sha}&branch=main&per_page=30"
    )
    return evaluate(local_branch, local_sha, local_clean, remote_sha, runs)


def main() -> int:
    if sys.argv[1:] not in ([], ["--json"]):
        print("Usage: python3 debian13/update_preflight.py [--json]", file=sys.stderr)
        return 2
    try:
        result = inspect()
    except PreflightError as exc:
        result = {
            "repository": REPOSITORY, "mode": "read_only_manual_update_preflight",
            "eligibleForManualReleaseReview": False,
            "deploymentPerformed": False, "productionReadinessVerified": False,
            "reason": str(exc),
        }
    if "--json" in sys.argv[1:]:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("Debian13_orginal — read-only release preflight")
        for key, value in result.get("checks", {}).items():
            print(f"  {'PASS' if value else 'BLOCKED'}: {key}")
        if result.get("reason"):
            print("  BLOCKED:", result["reason"])
        print("Manual release review:", "eligible" if result["eligibleForManualReleaseReview"] else "blocked")
        print("No checkout, deployment, restart or merge was performed.")
    return 0 if result["eligibleForManualReleaseReview"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
