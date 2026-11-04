"""Read-only, fail-closed adapter for the owner's real SVL Python state engine.

Uses legacy_svl_kernel.py imported from the owner's SVL_System repository.
No AI inference, kernel boot, shell, root, automatic sync or arbitrary command.
Never return chat messages, notes, operator identity or arbitrary config keys.
"""
from __future__ import annotations

import copy
import json
import os
import stat
from pathlib import Path

from legacy_svl_kernel import DEFAULT_STORE, migrate_model_config

LEGACY_STORE_NAME = ".svl_kernel_store.json"
MAX_STORE_BYTES = 256 * 1024
ALLOWED_PROFILES = {"Alex", "Aria", "Astyria", "Aurelius", "si_1"}


def _baseline() -> dict:
    return {
        "source": "mmitek515-netizen/SVL_System/svl_kernel.py",
        "kind": "svl_python_legacy_state_engine",
        "sourceAvailable": True,
        "storePresent": False,
        "stateValid": False,
        "configMigratedInMemory": False,
        "profileNamesConfigured": [],
        "profilesAreActiveWorkers": False,
        "operatingSystemBootVerified": False,
        "modelInferenceVerified": False,
        "cloudSyncVerified": False,
        "readOnly": True,
    }


def _safe_string(value: object, limit: int = 80) -> str:
    return value.strip()[:limit] if isinstance(value, str) else "unknown"


def _load_file(path: Path) -> object:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_STORE_BYTES:
            raise ValueError("unsafe_state")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_STORE_BYTES + 1)
        if len(data) > MAX_STORE_BYTES:
            raise ValueError("oversized_state")
        return json.loads(data)
    finally:
        os.close(descriptor)


def inspect_svl(workspace: Path) -> dict:
    """Inspect only the private Debian13 workspace, without any file writes."""
    result = _baseline()
    path = Path(workspace) / LEGACY_STORE_NAME
    try:
        if path.is_symlink():
            result["error"] = "legacy_state_symlink_refused"
            return result
        if path.exists():
            result["storePresent"] = True
            state = _load_file(path)
        else:
            state = copy.deepcopy(DEFAULT_STORE)
        if not isinstance(state, dict):
            raise ValueError("invalid_state")
        config = state.get("config")
        history = state.get("update_history")
        messages = state.get("chat_history")
        if not isinstance(config, dict) or not isinstance(history, list) or not isinstance(messages, list):
            raise ValueError("invalid_state")
        sanitized = copy.deepcopy(config)
        # Reuse the actual user's Python engine migration logic without allowing
        # its CLI to replace or rewrite the live JSON state.
        migrated = migrate_model_config(sanitized)
        profiles = sanitized.get("active_profiles", [])
        if not isinstance(profiles, list):
            raise ValueError("invalid_profiles")
        result.update({
            "stateValid": True,
            "configMigratedInMemory": migrated,
            "storeType": "persisted" if result["storePresent"] else "defaults_only",
            "version": _safe_string(sanitized.get("sys_version")),
            "mode": _safe_string(sanitized.get("mode")),
            "profileNamesConfigured": list(dict.fromkeys(
                name for name in profiles if isinstance(name, str) and name in ALLOWED_PROFILES
            ))[:5],
            "chatHistoryEntries": len(messages),
            "updateEntries": len(history),
        })
    except (OSError, ValueError, UnicodeError, TypeError, OverflowError):
        result["error"] = "legacy_state_invalid_or_unavailable"
    return result

def terminal_svl_report(workspace: Path, action: str) -> dict:
    """Allowlisted, non-executing SVL utility for the private Debian13 terminal.

    Only consumes sanitized data from inspect_svl(); no user content, tokens,
    arbitrary JSON fields or real shell commands are exposed or processed.
    """
    if action not in {"status", "report", "profiles"}:
        raise ValueError("invalid_svl_action")
    result = inspect_svl(workspace)
    if not result["stateValid"]:
        return {
            "exitCode": 1,
            "stdout": "",
            "stderr": "SVL: stan nieprawidłowy lub niedostępny; brak zmian danych.\n",
        }
    if action == "profiles":
        content = (
            "Profile skonfigurowane w SVL (NIE są to uruchomieni agenci):\n"
            + "\n".join(result["profileNamesConfigured"])
            + "\n"
        )
    elif action == "report":
        allowed = (
            "source", "kind", "version", "mode", "storePresent",
            "stateValid", "configMigratedInMemory", "chatHistoryEntries",
            "updateEntries", "profilesAreActiveWorkers",
            "operatingSystemBootVerified", "modelInferenceVerified",
            "cloudSyncVerified", "readOnly",
        )
        content = json.dumps(
            {key: result.get(key) for key in allowed},
            ensure_ascii=False, indent=2,
        ) + "\n"
    else:
        content = (
            "SVL Python: " + result["version"] + "\n"
            + "Tryb: " + result["mode"] + "\n"
            + "Źródło: " + result["source"] + "\n"
            + "Magazyn: " + ("plik" if result["storePresent"] else "ustawienia domyślne") + "\n"
            + "Nie uruchomiono jądra Debian ani agentów AI.\n"
        )
    return {"exitCode": 0, "stdout": content, "stderr": ""}
