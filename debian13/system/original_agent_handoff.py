"""Opt-in handoff to the ORIGINAL four AI Studio agents' reviewed-task API.

This is NOT an Issue-to-PR coding executor. A successful reply confirms
acceptance into a model review queue only. Remote execution is proved only by
reading a completed job with actual provider, model and result fields.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from alex_bridge import NoRedirect, SOURCE_REPO, existing_api_base

TARGET_REPO = "mmitek515-netizen/Debian_13"
REVIEW_CAPABILITY = "debian13_review_task_v1"
AGENTS = {"alex": "Alex", "aria": "Aria", "astyria": "Astyria", "aurelius": "Aurelius"}
KINDS = frozenset({"development", "bugfix", "audit", "research"})
REMOTE_ID = re.compile(r"\A[a-zA-Z0-9_-]{1,128}\Z")
MAX_RESPONSE = 32768


class OriginalAgentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _base() -> str:
    try:
        base = existing_api_base()
    except ValueError:
        raise OriginalAgentError("invalid_source", "Original agent API URL is invalid.") from None
    if not base:
        raise OriginalAgentError("source_not_configured",
                                 "Original agents API is not configured.")
    return base


def _token() -> str:
    token = (os.getenv("ORIGINAL_AGENTS_API_TOKEN")
             or os.getenv("ALEX_EXISTING_API_TOKEN") or "")
    if not 32 <= len(token) <= 512 or any(not 33 <= ord(c) <= 126 for c in token):
        raise OriginalAgentError("missing_source_token",
                                 "Original agents API requires a private bearer token.")
    return token


def _request(method: str, path: str, payload: dict | None = None,
             idempotency_key: str | None = None) -> dict:
    allowed = {"/agent-tasks/capabilities", "/agent-tasks/workers",
               "/agent-tasks/submit"}
    if path not in allowed and not (
            method == "GET" and path.startswith("/agent-tasks/jobs/")
            and REMOTE_ID.fullmatch(path[len("/agent-tasks/jobs/"):])):
        raise OriginalAgentError("invalid_route", "Unsupported agent API route.")
    if (method, path) not in {("GET", "/agent-tasks/capabilities"),
                             ("GET", "/agent-tasks/workers"),
                             ("POST", "/agent-tasks/submit")} and not (
                             method == "GET" and path.startswith("/agent-tasks/jobs/")):
        raise OriginalAgentError("invalid_route", "Unsupported agent API method.")
    headers = {"Authorization": "Bearer " + _token(), "Accept": "application/json"}
    body = None
    if payload is not None:
        if method != "POST":
            raise OriginalAgentError("invalid_request", "Only POST accepts data.")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > 8192:
            raise OriginalAgentError("request_too_large", "Agent task exceeds the limit.")
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        if not REMOTE_ID.fullmatch(idempotency_key):
            raise OriginalAgentError("invalid_id", "Invalid task identifier.")
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(_base() + path, data=body,
                                 method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=8) as response:
            if response.status != (202 if method == "POST" else 200):
                raise OriginalAgentError("source_error", "Unexpected original agent status.")
            raw = response.read(MAX_RESPONSE + 1)
    except (OSError, urllib.error.URLError, TimeoutError):
        raise OriginalAgentError("source_unavailable",
                                 "Original agent source is unavailable or unauthorized.") from None
    if len(raw) > MAX_RESPONSE:
        raise OriginalAgentError("response_too_large", "Agent response exceeds the limit.")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise OriginalAgentError("invalid_response", "Invalid original agent response.") from None
    if not isinstance(result, dict):
        raise OriginalAgentError("invalid_response", "Invalid original agent response.")
    return result


def capabilities() -> dict:
    if not (os.getenv("ALEX_EXISTING_API_URL") or os.getenv("BASTILLA_REMOTE_API_URL")):
        return {"configured": False, "supported": False, "allowed": False,
                "reason": "source_not_configured", "agentRuntimeVerified": False}
    if os.getenv("ORIGINAL_AGENTS_ALLOW_REVIEW") != "1":
        return {"configured": True, "supported": False, "allowed": False,
                "reason": "operator_opt_in_required", "agentRuntimeVerified": False}
    response = _request("GET", "/agent-tasks/capabilities")
    accepted = (
        response.get("sourceRepository") == SOURCE_REPO
        and response.get("targetRepository") == TARGET_REPO
        and response.get("protocolVersion") == 1
        and response.get("mode") == "model_review_only"
        and response.get("assignedRoles") is False
        and response.get("customInstructions") is False
        and isinstance(response.get("agents"), list)
        and set(response["agents"]) == set(AGENTS.values())
        and isinstance(response.get("capabilities"), list)
        and REVIEW_CAPABILITY in response["capabilities"])
    return {"configured": True, "supported": accepted, "allowed": True,
            "reason": "review_protocol_ready" if accepted else "protocol_mismatch",
            "targetRepository": TARGET_REPO, "agentRuntimeVerified": False,
            "mode": "model_review_only"}


def submit(task: dict) -> dict:
    if (not isinstance(task, dict) or task.get("agent") not in AGENTS
            or task.get("kind") not in KINDS
            or task.get("status") != "queued"
            or task.get("execution") != "not_started"
            or not isinstance(task.get("id"), str)
            or not REMOTE_ID.fullmatch(task["id"])):
        raise OriginalAgentError("invalid_task", "Task is not eligible for original AI review.")
    c = capabilities()
    if not c["supported"]:
        raise OriginalAgentError("unsupported_protocol", "Original AI review protocol is unavailable.")
    payload = {"agent": AGENTS[task["agent"]],
               "capability": REVIEW_CAPABILITY,
               "task": {"id": task["id"], "title": task["title"],
                        "description": task["description"], "kind": task["kind"],
                        "targetRepository": TARGET_REPO},
               "mode": "pull_request_proposal", "purchaseAuthorized": False,
               "autoMerge": False}
    reply = _request("POST", "/agent-tasks/submit", payload, idempotency_key=task["id"])
    remote_id = reply.get("taskId")
    if (reply.get("accepted") is not True or reply.get("agent") != payload["agent"]
            or reply.get("capability") != REVIEW_CAPABILITY
            or not isinstance(remote_id, str) or not REMOTE_ID.fullmatch(remote_id)):
        raise OriginalAgentError("unverified_receipt",
                                 "Original agent source did not confirm task acceptance.")
    return {"acceptedByRemoteApi": True, "remoteTaskId": remote_id,
            "agentExecutionVerified": False, "pullRequestVerified": False,
            "mode": "model_review_only"}


def status(remote_id: str, expected_agent: str) -> dict:
    if not isinstance(remote_id, str) or not REMOTE_ID.fullmatch(remote_id):
        raise OriginalAgentError("invalid_id", "Invalid original agent job ID.")
    reply = _request("GET", "/agent-tasks/jobs/" + remote_id)
    if reply.get("id") != remote_id or reply.get("agent") != AGENTS.get(expected_agent):
        raise OriginalAgentError("job_mismatch", "Original agent job identity mismatch.")
    state = reply.get("status")
    if state not in ("queued", "running", "completed", "failed"):
        raise OriginalAgentError("invalid_job_status", "Unrecognized agent job state.")
    verified = (state == "completed" and reply.get("workerExecutionVerified") is True
                and isinstance(reply.get("provider"), str) and bool(reply["provider"])
                and isinstance(reply.get("model"), str) and bool(reply["model"])
                and isinstance(reply.get("result"), str) and bool(reply["result"]))
    return {"remoteTaskId": remote_id, "agent": expected_agent, "status": state,
            "agentExecutionVerified": bool(verified),
            "provider": reply.get("provider") if verified else None,
            "model": reply.get("model") if verified else None,
            "result": reply["result"][:16000] if verified else None,
            "errorCode": (str(reply["errorCode"])[:100] if state == "failed"
                          and reply.get("errorCode") else None),
            "pullRequestVerified": False,
            "meaning": "A completed model review is NOT a code commit or a pull request."}
