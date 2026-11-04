"""Offline-only regression tests for the SVL read-only diagnostic importer."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from svl_report_adapter import SVLReportError, MAX_INPUT_BYTES, parse_report


def example():
    return {
        "Timestamp": "2026-11-05 00:00:00",
        "System": "5.6.0-QuantumWheel",
        "Operator": "PRIVATE USER NAME",
        "Performance": "+1456% (unverified claim)",
        "Compact_Metric_C": 2.5,
        "Actual_SI_Updates_Count": 3,
        "Config_Snapshot": {"root_privileges": True, "api_token": "DO NOT EXPOSE"},
    }


class SVLReportAdapterTests(unittest.TestCase):
    def test_accepts_genuine_svl_report_shape_without_privileged_claims(self):
        report = parse_report(json.dumps(example()).encode())
        self.assertTrue(report["parsedSuccessfully"])
        self.assertEqual(report["reportedKernelVersion"], "5.6.0-QuantumWheel")
        self.assertEqual(report["reportedCompactMetricC"], 2.5)
        self.assertEqual(report["reportedHistoryEntries"], 3)
        for claim in ("independentExecutionVerified", "operatingSystemBootVerified",
                      "privilegedAccessVerified", "performanceBoostVerified",
                      "originalAgentsExecutionVerified"):
            self.assertIs(report[claim], False)
        dumped = json.dumps(report)
        self.assertNotIn("PRIVATE USER NAME", dumped)
        self.assertNotIn("DO NOT EXPOSE", dumped)
        self.assertNotIn("1456", dumped)

    def test_rejects_invalid_reports_without_loading_code(self):
        inputs = [
            b'not json', b'[]', b'{}', b'{' + b'a' * MAX_INPUT_BYTES + b'}',
            json.dumps({**example(), "Compact_Metric_C": "2.5"}).encode(),
            json.dumps({**example(), "Compact_Metric_C": float('nan')}).encode(),
            json.dumps({**example(), "Actual_SI_Updates_Count": True}).encode(),
            json.dumps({**example(), "System": "bad\nversion"}).encode(),
        ]
        for raw in inputs:
            with self.subTest(raw=raw[:60]), self.assertRaises(SVLReportError):
                parse_report(raw)

    def test_ignores_untrusted_commands_and_secret_fields(self):
        data = example()
        data["command"] = "rm -rf /"
        data["shell"] = {"execute": True}
        data["originalAgentsExecutionVerified"] = True
        result = parse_report(json.dumps(data))
        self.assertFalse(result["originalAgentsExecutionVerified"])
        self.assertNotIn("command", json.dumps(result))
        self.assertNotIn("shell", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
