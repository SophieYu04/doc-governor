from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from docgov.github import MARKER, report_markdown, upsert_pr_comment
from docgov.models import GovernanceDecision


ROOT = Path(__file__).resolve().parents[1]


class ActionContractTests(unittest.TestCase):
    def test_composite_action_exposes_stable_outputs_and_commits_only_modified_paths(self) -> None:
        action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
        self.assertEqual(
            set(action["outputs"]),
            {"result", "changed", "finding_count", "decision_json", "commit_sha"},
        )
        source = (ROOT / "action.yml").read_text(encoding="utf-8")
        self.assertIn('json.load(handle).get("modified_paths", [])', source)
        self.assertNotIn("git add .", source)
        self.assertIn('if [ "$result" = "action_required" ] || [ "$result" = "blocked" ]', source)
        self.assertIn("supabase_projects", action["inputs"])
        self.assertIn("supabase_evidence_dir", action["inputs"])
        self.assertIn('--supabase-projects "$DOCGOV_SUPABASE_PROJECTS"', source)

    def test_workflows_pin_actions_and_never_use_pull_request_target(self) -> None:
        workflow_sources = [path.read_text(encoding="utf-8") for path in (ROOT / ".github/workflows").glob("*.yml")]
        combined = "\n".join(workflow_sources)
        self.assertNotIn("pull_request_target", combined)
        for use in re.findall(r"uses:\s*([^\s#]+)", combined + "\n" + (ROOT / "action.yml").read_text(encoding="utf-8")):
            if use.startswith("SophieYu04/doc-governor@"):
                continue
            self.assertRegex(use, r"^[^@]+@[0-9a-f]{40}$")

    def test_fork_model_and_oidc_steps_are_same_repository_only(self) -> None:
        source = (ROOT / ".github/workflows/docgov-review.yml").read_text(encoding="utf-8")
        same_repo = "github.event.pull_request.head.repo.full_name == github.repository"
        self.assertGreaterEqual(source.count(same_repo), 2)
        self.assertIn("persist-credentials: false", source)
        self.assertIn("vars.DOCGOV_AWS_ROLE_ARN != ''", source)
        self.assertIn("id-token: write", source)

    def test_decision_card_comment_is_updated_instead_of_duplicated(self) -> None:
        decision = GovernanceDecision("run", "review", "pass", False)
        existing = [{"id": 17, "body": MARKER, "user": {"type": "Bot"}}]
        with patch("docgov.github._request", side_effect=[existing, None]) as request:
            upsert_pr_comment(decision, "owner/repo", "3", "token")
        self.assertEqual(request.call_args_list[1].args[0], "PATCH")
        self.assertTrue(request.call_args_list[1].args[1].endswith("/17"))

    def test_blocked_model_error_is_visible_in_decision_card(self) -> None:
        decision = GovernanceDecision("run", "review", "blocked", False, error="model timeout")
        report = report_markdown(decision)
        self.assertIn("**Error:** model timeout", report)
        self.assertNotIn("No documentation drift", report)

    def test_init_result_uses_the_stable_action_result_enum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [sys.executable, "-m", "docgov", "--root", temporary, "--json", "init"],
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
        payload = yaml.safe_load(completed.stdout)
        self.assertEqual(payload["result"], "pass")
        self.assertTrue(payload["proposal_only"])


if __name__ == "__main__":
    unittest.main()
