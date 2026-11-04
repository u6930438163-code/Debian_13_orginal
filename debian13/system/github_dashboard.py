"""Read-only GitHub REST snapshot for the private Debian 13 operator dashboard.

This is a separate server-side integration. A GitHub plugin connected to ChatGPT
does NOT automatically authorize this Debian machine. The default public repo
works without credentials; optional server-side DEBIAN13_GITHUB_TOKEN is never
included in responses or logs. No mutations or arbitrary destination URLs.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_REPO = "mmitek515-netizen/Debian_13"
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}\Z")
MAX_REPLY_BYTES = 256 * 1024
USER_AGENT = "Debian13-Connect-readonly/0.1"
ENDPOINTS = {
    "pullRequests": "pulls?state=open&per_page=12",
    "issues": "issues?state=open&per_page=12",
    "runs": "actions/runs?per_page=12",
}


class GitHubUnavailable(Exception):
    """Safe error with no credentials, response bodies, or private paths."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def repository() -> str:
    value = os.environ.get("DEBIAN13_GITHUB_REPO", DEFAULT_REPO).strip()
    if not REPO_PATTERN.fullmatch(value) or ".." in value:
        raise GitHubUnavailable("Invalid configured GitHub repository.")
    return value


def _fetch_json(repo: str, endpoint: str, token: str, opener=None):
    # Only fixed GitHub REST endpoints; never interpolate a user-provided URL.
    if endpoint not in ENDPOINTS.values():
        raise GitHubUnavailable("Unsupported GitHub API endpoint.")
    safe_repo = "/".join(urllib.parse.quote(part, safe="") for part in repo.split("/"))
    url = "https://api.github.com/repos/" + safe_repo + "/" + endpoint
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        if "\r" in token or "\n" in token:
            raise GitHubUnavailable("Invalid GitHub credential.")
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers, method="GET")
    opener = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=3) as response:
            if response.status != 200:
                raise GitHubUnavailable("GitHub returned an unexpected status.")
            raw = response.read(MAX_REPLY_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError):
        raise GitHubUnavailable("GitHub API is unreachable or denied access.") from None
    if len(raw) > MAX_REPLY_BYTES:
        raise GitHubUnavailable("GitHub API response exceeded the size limit.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise GitHubUnavailable("GitHub API returned invalid JSON.") from None


def _link(value, repo: str) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme == "https" and parsed.hostname == "github.com"
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and parsed.path.startswith("/" + repo + "/")):
        return value
    return None


def _summarize(kind: str, raw, repo: str) -> list[dict]:
    if kind == "runs":
        raw = raw.get("workflow_runs") if isinstance(raw, dict) else None
    if not isinstance(raw, list):
        raise GitHubUnavailable("GitHub API returned an unexpected data shape.")
    result = []
    for obj in raw[:12]:
        if not isinstance(obj, dict):
            continue
        if kind == "issues" and "pull_request" in obj:
            continue
        ident = obj.get("number") if kind != "runs" else obj.get("id")
        title = obj.get("title") if kind != "runs" else obj.get("name")
        link = _link(obj.get("html_url"), repo)
        if not isinstance(ident, int) or not isinstance(title, str) or not link:
            continue
        item = {"number": ident, "title": title[:180], "url": link,
                "state": str(obj.get("state", "unknown"))[:40],
                "updatedAt": str(obj.get("updated_at", ""))[:40]}
        if kind == "pullRequests":
            item["draft"] = obj.get("draft") is True
        if kind == "runs":
            item["status"] = str(obj.get("status", "unknown"))[:40]
            item["conclusion"] = (str(obj["conclusion"])[:40]
                                  if obj.get("conclusion") is not None else None)
            item["branch"] = str(obj.get("head_branch", ""))[:100]
        result.append(item)
    return result


def fetch_dashboard(repo: str | None = None, token: str | None = None,
                    opener=None) -> dict:
    repo = repo or repository()
    if not REPO_PATTERN.fullmatch(repo) or ".." in repo:
        raise GitHubUnavailable("Invalid configured GitHub repository.")
    token = token if token is not None else os.environ.get("DEBIAN13_GITHUB_TOKEN", "")
    result = {"repository": repo, "repositoryUrl": "https://github.com/" + repo,
              "source": "GitHub REST API", "access": "server_side_read_only",
              "chatgptPluginConnected": False, "agentExecutionVerified": False,
              "observedAt": datetime.now(timezone.utc).isoformat(),
              "errors": {}, "pullRequests": [], "issues": [], "runs": []}
    successes = 0
    for name, endpoint in ENDPOINTS.items():
        try:
            result[name] = _summarize(
                name, _fetch_json(repo, endpoint, token, opener=opener), repo)
        except GitHubUnavailable as error:
            result["errors"][name] = str(error)
        else:
            successes += 1
    result["githubApiReachable"] = successes > 0
    result["partial"] = successes < len(ENDPOINTS)
    result["credentialConfigured"] = bool(token)
    return result


class DashboardCache:
    """Cache snapshots to avoid exhausting public GitHub's low anonymous quota."""
    def __init__(self, ttl_seconds: int = 120):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._until = 0.0
        self._value = None

    def get(self) -> dict:
        with self._lock:
            if self._value is not None and time.monotonic() < self._until:
                return self._value
            value = fetch_dashboard()
            if value["githubApiReachable"]:
                self._value = value
                self._until = time.monotonic() + self.ttl_seconds
            return value
