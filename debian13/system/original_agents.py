"""Read actual task registry for all FOUR original owner agents.

Source: u6930438163-code/AI_Studio_BastillaOS. Source task records are
observations, not proof that the corresponding autonomous LLM is executing.
Never synthesize worker activity from Copilot agent profile files.
"""
from __future__ import annotations

from alex_bridge import SOURCE_REPO, existing_api_base, fetch_registry

ORIGINAL_AGENTS = {
    "Alex": "alex",
    "Aria": "aria",
    "Astyria": "astyria",
    "Aurelius": "aurelius",
}


def report() -> dict:
    """GET /api/v1/original-agents: authenticated and read-only."""
    configured = existing_api_base() is not None
    base = {
        "sourceRepository": SOURCE_REPO,
        "registryConfigured": configured,
        "registryReachable": False,
        "originalAgentExecutionVerified": False,
        "meaning": (
            "Source agent profiles and registry do not prove an AI worker is running. "
            "Live agent execution must be independently verified."
        ),
        "agents": [
            {
                "id": slug,
                "name": name,
                "role": None,
                "assignment": None,
                "customInstructions": False,
                "sourceProfile": f".github/agents/{name}.agent.md",
                "profileAvailableInDebian13": True,
                "connected": False,
                "workerVerified": False,
                "tasksInRegistry": 0,
                "runningEntriesInRegistry": 0,
            }
            for name, slug in ORIGINAL_AGENTS.items()
        ],
    }
    if not configured:
        base["reason"] = "existing_source_api_not_configured"
        return base
    try:
        data = fetch_registry()
    except Exception:
        # Caller can distinguish unconfigured vs failed probe without revealing
        # secrets, internal source URLs or upstream exception contents.
        base["reason"] = "existing_source_api_unreachable_or_invalid"
        return base
    base["registryReachable"] = True
    base["reason"] = "registry_reachable_worker_unverified"
    tasks = data["tasks"]
    for entry in base["agents"]:
        source_name = entry["name"]
        matching = [
            task for task in tasks if isinstance(task, dict)
            and task.get("agent") == source_name
        ]
        entry["tasksInRegistry"] = len(matching)
        entry["runningEntriesInRegistry"] = sum(
            task.get("status") == "running" for task in matching
        )
        # Deliberately not setting connected=True: we only verified the
        # shared registry API, not the original agent worker.
    return base
