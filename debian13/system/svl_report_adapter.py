#!/usr/bin/env python3
"""Read-only adapter for output of the owner's original SVL topological kernel.

The input is untrusted JSON produced by a *separate* operator-run script.
This adapter never loads, evaluates or executes the SVL source, commands, URLs,
or claims of privileged access. It never treats status text as proof of a live OS.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

MAX_INPUT_BYTES = 16_384


class SVLReportError(ValueError):
    """Invalid/untrusted SVL report, with a non-sensitive error message."""


def parse_report(raw: bytes | str) -> dict:
    if isinstance(raw, str):
        payload = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        payload = raw
    else:
        raise SVLReportError("Expected UTF-8 JSON report")
    if len(payload) > MAX_INPUT_BYTES:
        raise SVLReportError("SVL report exceeds input limit")
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        raise SVLReportError("Invalid SVL JSON report") from None
    if not isinstance(data, dict):
        raise SVLReportError("SVL report must be a JSON object")
    version = data.get("System")
    metric = data.get("Compact_Metric_C")
    entries = data.get("Actual_SI_Updates_Count")
    if not isinstance(version, str) or not 1 <= len(version) <= 80:
        raise SVLReportError("Missing or invalid system version")
    if any(ord(char) < 32 or ord(char) == 127 for char in version):
        raise SVLReportError("Invalid version characters")
    if isinstance(metric, bool) or not isinstance(metric, (float, int)):
        raise SVLReportError("Invalid compact metric")
    if not math.isfinite(metric) or not 0 <= metric <= 4:
        raise SVLReportError("Compact metric out of expected range")
    if type(entries) is not int or not 0 <= entries <= 100_000:
        raise SVLReportError("Invalid history entry count")
    return {
        "source": "svl_topological_kernel_report",
        "mode": "read_only_diagnostic_import",
        "reportedKernelVersion": version,
        "reportedCompactMetricC": metric,
        "reportedHistoryEntries": entries,
        "parsedSuccessfully": True,
        "independentExecutionVerified": False,
        "operatingSystemBootVerified": False,
        "privilegedAccessVerified": False,
        "performanceBoostVerified": False,
        "originalAgentsExecutionVerified": False,
        "notes": "Values are claims and calculations from an external Python report, not evidence of running agents or hardware.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an SVL report without executing SVL code")
    parser.add_argument("--input", type=Path, help="JSON report file; default stdin")
    args = parser.parse_args(argv)
    try:
        if args.input:
            with args.input.open("rb") as file:
                raw = file.read(MAX_INPUT_BYTES + 1)
        else:
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        result = parse_report(raw)
    except (SVLReportError, OSError):
        print("ERROR: Invalid or unavailable SVL diagnostic report", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
