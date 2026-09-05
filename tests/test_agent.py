from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docgov.agent import (
    _StrandsTraceRecorder,
    _confine_model_finding,
    _validate_snapshot_tool_trace,
    invoke_strands,
)
from docgov.catalog import Catalog
from docgov.engine import RepositorySnapshot
from docgov.models import Finding, GovernanceDecision


class AgentPolicyTests(unittest.TestCase):
    def test_trace_records_one_event_per_tool_use_without_arguments(self) -> None:
        recorder = _StrandsTraceRecorder()
        event = {
            "toolUseId": "tool-1",
            "name": "repository_snapshot",
            "input": {"private_document": "must not be recorded"},
        }

        recorder(current_tool_use=event)
        recorder(current_tool_use=event)
        _validate_snapshot_tool_trace(recorder.events)
        recorder.record_model_complete("test-model")

        self.assertEqual(recorder.events, [
            {"event": "tool_call", "name": "repository_snapshot"},
            {"event": "model_complete", "name": "test-model"},
        ])
        self.assertNotIn("private_document", str(recorder.events))

    def test_trace_fails_closed_without_exactly_one_snapshot_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly once"):
            _validate_snapshot_tool_trace([])
        with self.assertRaisesRegex(ValueError, "exactly once"):
            _validate_snapshot_tool_trace([
                {"event": "tool_call", "name": "repository_snapshot"},
                {"event": "tool_call", "name": "repository_snapshot"},
            ])
        with self.assertRaisesRegex(ValueError, "exactly once"):
            _validate_snapshot_tool_trace([
                {"event": "tool_call", "name": "unbounded_repository"},
            ])

    def test_model_only_stale_mutation_becomes_a_human_block(self) -> None:
        finding = Finding(
            kind="stale",
            risk="low",
            action="mark_stale",
            documents=["docs/status/RELEASE.md"],
            reason="Model suspects drift.",
        )

        confined = _confine_model_finding(finding)

        self.assertEqual(confined.risk, "high")
        self.assertEqual(confined.action, "block")
        self.assertIsNotNone(confined.human_decision)

    def test_model_duplicate_still_requires_deterministic_merge_boundary(self) -> None:
        finding = Finding(
            kind="duplicate",
            risk="low",
            action="merge_new_file",
            documents=["docs/architecture/COPY.md", "docs/architecture/API.md"],
            reason="Semantic duplicate.",
        )

        self.assertIs(_confine_model_finding(finding), finding)
        self.assertEqual(finding.action, "merge_new_file")

    def test_model_trace_is_part_of_the_structured_decision(self) -> None:
        decision = GovernanceDecision(
            run_id="run",
            mode="review",
            result="pass",
            changed=False,
            model_used=True,
            model_trace=[{"event": "tool_call", "name": "repository_snapshot"}],
        )

        self.assertEqual(
            decision.to_dict()["model_trace"],
            [{"event": "tool_call", "name": "repository_snapshot"}],
        )

    def test_strands_invocation_proves_the_bounded_tool_call(self) -> None:
        class FakeAgent:
            def __init__(self, *, tools, callback_handler, **_kwargs) -> None:
                self.tools = tools
                self.callback_handler = callback_handler

            def __call__(self, _prompt, *, structured_output_model, **_kwargs):
                self.tools[0]()
                tool_event = {
                    "toolUseId": "tool-1",
                    "name": "repository_snapshot",
                    "input": {"omitted": "never copied into trace"},
                }
                self.callback_handler(current_tool_use=tool_event)
                self.callback_handler(current_tool_use=tool_event)
                return SimpleNamespace(structured_output=structured_output_model())

        snapshot = RepositorySnapshot(root=Path("."), catalog=Catalog.default())
        baseline = GovernanceDecision("run", "review", "pass", False)
        with (
            patch("strands.Agent", FakeAgent),
            patch("strands.tool", lambda function: function),
            patch("strands.models.BedrockModel", lambda **_kwargs: object()),
        ):
            decision = invoke_strands(snapshot, baseline, model_id="test-model")

        self.assertTrue(decision.model_used)
        self.assertEqual(decision.result, "pass")
        self.assertEqual(decision.model_trace, [
            {"event": "tool_call", "name": "repository_snapshot"},
            {"event": "model_complete", "name": "test-model"},
        ])


if __name__ == "__main__":
    unittest.main()
