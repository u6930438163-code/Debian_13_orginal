"""Read-only diagnostic for an independently installed Codex CLI.

The API does not run coding tasks, expose auth files, or infer ChatGPT plan access.
The probe reports status only for the user running the API process.
"""
from __future__ import annotations

import re
import shutil
import subprocess


def _probe(command: list[str]) -> tuple[int | None, str]:
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return result.returncode, (result.stdout + "\n" + result.stderr)[:2048]


def report() -> dict:
    exe = shutil.which("codex")
    base = {
        "module": "codex",
        "separateFromDialogTriady": True,
        "cliInstalled": exe is not None,
        "version": None,
        "authentication": "not_checked",
        "cloudWorkerVerified": False,
        "codexProcessRunning": False,
        "githubIssue": "https://github.com/mmitek515-netizen/Debian_13/issues/3",
        "description": "Status dotyczy tylko hosta API; Codex z Codespaces może działać na innym hoście.",
    }
    if exe is None:
        base["authentication"] = "not_installed"
        return base

    code, version = _probe([exe, "--version"])
    if code == 0:
        match = re.search(r"(?i)\bcodex\b[^\r\n]{0,80}", version)
        if match:
            base["version"] = match.group(0).strip()[:100]
    login_code, output = _probe([exe, "login", "status"])
    if login_code == 0:
        base["authentication"] = "authenticated"
    elif login_code is None:
        base["authentication"] = "check_unavailable"
    elif any(term in output.lower() for term in
             ("not logged", "not authenticated", "not signed in", "login required")):
        base["authentication"] = "not_authenticated"
    else:
        base["authentication"] = "unknown"
    return base
