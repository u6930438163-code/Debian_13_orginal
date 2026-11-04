"""Request an analysis from an original AI worker; never execute generated code."""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from original_agent_handoff import AGENTS, OriginalAgentError, status, submit


def generate(agent: str, description: str, *, timeout: float = 240,
             interval: float = 3) -> str:
    if agent not in AGENTS or not isinstance(description, str) or not description.strip() or len(description) > 2000:
        raise ValueError("Select an original agent and a task of 1..2000 characters.")
    if not 0 < timeout <= 480 or not 0 < interval <= 10:
        raise ValueError("Invalid polling limits.")
    task = {"id": "github-" + uuid.uuid4().hex, "agent": agent,
            "kind": "development", "status": "queued", "execution": "not_started",
            "title": description.strip()[:100], "description": description.strip()}
    receipt = submit(task)
    remote_id = receipt["remoteTaskId"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = status(remote_id, agent)
        if result["status"] == "failed":
            raise OriginalAgentError("worker_failed", "Original AI worker failed; no plan published.")
        if result["status"] == "completed":
            if (result.get("agentExecutionVerified") is not True
                    or result.get("provider") != "ollama"
                    or not isinstance(result.get("result"), str)
                    or not result["result"].strip()):
                raise OriginalAgentError("unverified_model", "A verified original Ollama result is required.")
            return ("# Original agent analysis\n\n"
                    + "Agent: " + AGENTS[agent] + "\n\n"
                    + "Remote task: " + remote_id + "\n\n"
                    + "Provider: ollama\n\n"
                    + "Analysis only; no code execution, commit or PR was performed.\n\n"
                    + result["result"])
        time.sleep(interval)
    raise OriginalAgentError("worker_timeout", "Original AI has not completed; receipt is not execution.")


def main() -> int:
    try:
        report = generate(os.getenv("AGENT", ""), os.getenv("TASK", ""))
    except (OriginalAgentError, ValueError):
        print("BLOCKED: original Ollama worker did not return a verified analysis. Check private runtime configuration.")
        return 1
    Path("agent-plan.md").write_text(report, encoding="utf-8")
    print("Verified original Ollama analysis saved for review; no generated code executed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
