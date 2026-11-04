import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import original_agent_plan as plan


class OriginalAgentPlanTests(unittest.TestCase):
    def test_queued_receipt_waits_for_verified_ollama_result(self):
        completed = {"status": "completed", "agentExecutionVerified": True,
                     "provider": "ollama", "result": "Review of the project"}
        with patch.object(plan, "submit", return_value={"remoteTaskId": "job-1"}) as submit, \
             patch.object(plan, "status", side_effect=[{"status": "queued"}, completed]), \
             patch.object(plan.time, "sleep"):
            report = plan.generate("aria", "Review integrations")
        self.assertIn("Aria", report)
        self.assertIn("Review of the project", report)
        self.assertEqual(submit.call_args.args[0]["agent"], "aria")

    def test_unverified_or_paid_provider_results_are_rejected(self):
        for provider, verified in [("openai", True), ("ollama", False)]:
            with self.subTest(provider=provider, verified=verified), \
                 patch.object(plan, "submit", return_value={"remoteTaskId": "job-1"}), \
                 patch.object(plan, "status", return_value={"status": "completed",
                     "agentExecutionVerified": verified, "provider": provider, "result": "text"}):
                with self.assertRaises(plan.OriginalAgentError):
                    plan.generate("alex", "Review integrations")

    def test_failed_worker_does_not_produce_report(self):
        with patch.object(plan, "submit", return_value={"remoteTaskId": "job-1"}), \
             patch.object(plan, "status", return_value={"status": "failed"}):
            with self.assertRaises(plan.OriginalAgentError):
                plan.generate("astyria", "Review integrations")

    def test_timeout_does_not_mark_queue_as_execution(self):
        with patch.object(plan, "submit", return_value={"remoteTaskId": "job-1"}), \
             patch.object(plan.time, "monotonic", side_effect=[0, 1000]):
            with self.assertRaises(plan.OriginalAgentError):
                plan.generate("aurelius", "Review integrations")

    def test_invalid_task_is_rejected_before_network(self):
        with patch.object(plan, "submit") as submit:
            for agent, task in [("copilot", "review"), ("alex", " "), ("alex", "x" * 2001)]:
                with self.assertRaises(ValueError):
                    plan.generate(agent, task)
            submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
