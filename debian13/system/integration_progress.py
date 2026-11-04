"""Evidence-based local integration readiness for Debian13_orginal.

Scores describe observable integration checkpoints, NOT overall OS completion.
Do not equate an agent profile, a workflow file, or a successful build with a
live worker, user authentication, model inference, or ChatGPT connection.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def _local_ollama_endpoint(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme == "http"
                and parts.hostname in {"127.0.0.1", "localhost", "::1", "ollama"}
                and parts.port == 11434
                and parts.path == "/v1/chat/completions"
                and not parts.username and not parts.password
                and not parts.query and not parts.fragment)
    except ValueError:
        return False


def _module(identifier: str, name: str, checks: list[tuple[str, bool]]) -> dict:
    evidence = [{"id": key, "verified": value is True} for key, value in checks]
    verified = sum(item["verified"] for item in evidence)
    return {
        "id": identifier,
        "name": name,
        "percentage": 100 * verified // len(evidence),
        "verifiedCount": verified,
        "totalCount": len(evidence),
        "checkpoints": evidence,
        "connected": evidence[-1]["verified"],
    }


def report(*, codex: dict, ollama: dict, original_agents: dict,
           model_chat_url: str = "") -> dict:
    """Compute a conservative, local-only snapshot; never contact providers.

    Each of five integrations has four equally weighted *named* checkpoints.
    The final checkpoint is reserved for verified end-to-end execution. A
    configured registry or running local Ollama process does not prove an
    original agent performed work or that a model produced a real response.
    """
    modules = [
        _module("mcp", "MCP", [
            ("read_only_protocol_implemented", True),
            ("authenticated_local_http_available", True),
            ("client_handshake_verified", False),
            ("remote_https_client_verified", False),
        ]),
        _module("gpt_actions", "GPT Actions", [
            ("openapi_contract_implemented", True),
            ("https_deployment_verified", False),
            ("gpt_registration_verified", False),
            ("external_roundtrip_verified", False),
        ]),
        _module("codex", "Codex", [
            ("status_probe_implemented", True),
            ("cli_installed_on_api_host", codex.get("cliInstalled") is True),
            ("cli_authenticated_on_api_host", codex.get("authentication") == "authenticated"),
            ("independent_coding_worker_verified", codex.get("cloudWorkerVerified") is True),
        ]),
        _module("ollama", "Ollama lokalna", [
            ("local_worker_implemented", True),
            ("private_model_endpoint_configured", _local_ollama_endpoint(model_chat_url)),
            ("worker_heartbeat_fresh", ollama.get("workerHeartbeatVerified") is True),
            ("real_inference_verified", ollama.get("executionVerified") is True),
        ]),
        _module("original_agents", "Alex / Aria / Astyria / Aurelius", [
            ("original_agent_connector_implemented", True),
            ("source_registry_configured", original_agents.get("registryConfigured") is True),
            ("source_registry_reachable", original_agents.get("registryReachable") is True),
            ("original_agents_execution_verified",
             original_agents.get("originalAgentExecutionVerified") is True),
        ]),
    ]
    passed = sum(item["verifiedCount"] for item in modules)
    total = sum(item["totalCount"] for item in modules)
    return {
        "kind": "local_integration_checklist",
        "percentage": 100 * passed // total,
        "verifiedCount": passed,
        "totalCount": total,
        "modules": modules,
        "weighting": "Each of 20 named evidence checkpoints has equal weight.",
        "notOsCompletion": True,
        "notDeploymentProof": True,
        "remoteClientProbesPerformed": False,
    }
