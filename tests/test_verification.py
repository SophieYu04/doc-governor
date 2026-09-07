from __future__ import annotations

import json
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from docgov.catalog import Catalog
from docgov.cli import main as cli_main
from docgov.ledger import Ledger
from docgov.verification import (
    capture_inputs,
    capture_environment,
    definition_hash,
    import_verifications,
    run_verification,
    verification_status,
)


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        write(self.root / "src/a.txt", "a\n")
        write(self.root / "other/b.txt", "b\n")
        self.catalog_path = self.root / ".docgov/catalog.yaml"
        self.ledger_path = self.root / ".docgov/ledger.jsonl"
        self.command = [sys.executable, "-c", "raise SystemExit(0)"]
        write(self.catalog_path, json.dumps({
            "version": 1,
            "taxonomy": {},
            "documents": [],
            "verifications": [
                {
                    "id": "a-check",
                    "command": self.command,
                    "inputs": ["src/**"],
                    "depends_on": ["config/**"],
                    "related_documents": ["README.md"],
                    "environment": {"packages": []},
                },
                {
                    "id": "b-check",
                    "command": self.command,
                    "inputs": ["other/**"],
                },
            ],
            "policies": {},
        }))
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def catalog(self) -> Catalog:
        return Catalog.load(self.catalog_path)

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.ledger_path)

    def test_success_is_reusable_and_query_does_not_rerun(self) -> None:
        run_verification(self.root, self.catalog, self.ledger, "a-check")
        first = verification_status(self.root, self.catalog, self.ledger, "a-check")
        second = verification_status(self.root, self.catalog, self.ledger, "a-check")
        self.assertTrue(first["reusable"])
        self.assertEqual(second["execution_count"], 1)
        self.assertEqual(second["rerun"]["command"], self.command)

    def test_only_the_scope_that_changed_is_invalidated(self) -> None:
        run_verification(self.root, self.catalog, self.ledger, "a-check")
        run_verification(self.root, self.catalog, self.ledger, "b-check")
        write(self.root / "src/a.txt", "changed\n")
        self.assertFalse(verification_status(self.root, self.catalog, self.ledger, "a-check")["reusable"])
        self.assertTrue(verification_status(self.root, self.catalog, self.ledger, "b-check")["reusable"])

    def test_new_untracked_and_deleted_inputs_are_reported(self) -> None:
        run_verification(self.root, self.catalog, self.ledger, "a-check")
        write(self.root / "src/new.txt", "new\n")
        (self.root / "src/a.txt").unlink()
        status = verification_status(self.root, self.catalog, self.ledger, "a-check")
        self.assertEqual(status["affected_files"], ["src/a.txt", "src/new.txt"])

    def test_dependency_change_is_reported(self) -> None:
        write(self.root / "config/tool.json", "{}\n")
        run_verification(self.root, self.catalog, self.ledger, "a-check")
        write(self.root / "config/tool.json", '{"changed":true}\n')
        status = verification_status(self.root, self.catalog, self.ledger, "a-check")
        self.assertEqual(status["affected_files"], ["config/tool.json"])

    def test_environment_difference_fails_closed(self) -> None:
        run_verification(self.root, self.catalog, self.ledger, "a-check")
        changed = capture_environment([])
        changed["architecture"] = "different"
        with patch("docgov.verification.capture_environment", return_value=changed):
            status = verification_status(self.root, self.catalog, self.ledger, "a-check")
        self.assertFalse(status["reusable"])
        self.assertFalse(status["environment_matches"])

    def test_inputs_changed_during_run_are_never_reusable(self) -> None:
        catalog = self.catalog
        catalog.verification_for("a-check").command = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('src/a.txt').write_text('during\\n')",
        ]
        event = run_verification(self.root, catalog, self.ledger, "a-check")
        self.assertEqual(event["result"], "invalidated")
        self.assertFalse(verification_status(self.root, catalog, self.ledger, "a-check")["reusable"])

    def test_latest_failure_hides_an_older_success(self) -> None:
        run_verification(self.root, self.catalog, self.ledger, "a-check")
        catalog = self.catalog
        catalog.verification_for("a-check").command = [sys.executable, "-c", "raise SystemExit(3)"]
        run_verification(self.root, catalog, self.ledger, "a-check")
        status = verification_status(self.root, catalog, self.ledger, "a-check")
        self.assertFalse(status["reusable"])
        self.assertIn("did not succeed", status["reason"])

    def test_import_is_idempotent_and_unknown_environment_is_not_reusable(self) -> None:
        record = self.catalog.verification_for("a-check")
        captured = capture_inputs(self.root, record, ledger_path=self.ledger_path)
        event = {
            "verification": "a-check",
            "definition_hash": definition_hash(record),
            "command": self.command,
            "workdir": ".",
            "input_patterns": ["src/**"],
            "dependency_patterns": ["config/**"],
            "input_files": captured["inputs"],
            "dependency_files": captured["dependencies"],
            "source_commit": "abc",
            "started_at": "2026-09-06T00:00:00Z",
            "completed_at": "2026-09-06T00:01:00Z",
            "exit_code": 0,
            "result": "success",
            "environment": {
                "os": None,
                "os_release": None,
                "architecture": None,
                "python": "3.12.14",
                "packages": {},
            },
            "source": {
                "method": "github_actions",
                "url": "https://example.test/run/1",
                "digest": "a" * 64,
            },
            "raw_log": "SECRET DOCUMENT CONTENT",
            "model_trace": [
                {"event": "tool_call", "name": "auditor:evidence", "reasoning": "SECRET"},
                {"event": "tool_call", "name": "SECRET\nDOCUMENT"},
            ],
        }
        self.assertIsNotNone(record)
        self.assertEqual(import_verifications(self.ledger, event)["imported"], 1)
        self.assertEqual(import_verifications(self.ledger, event)["skipped"], 1)
        serialized = self.ledger_path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET DOCUMENT CONTENT", serialized)
        self.assertNotIn("SECRET\\nDOCUMENT", serialized)
        self.assertNotIn('"reasoning"', serialized)
        status = verification_status(self.root, self.catalog, self.ledger, "a-check")
        self.assertFalse(status["reusable"])
        self.assertIn("environment", status["reason"])

    def test_legacy_document_entries_remain_compatible(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(
            json.dumps({"document": "README.md", "action": "verify_current"}) + "\n",
            encoding="utf-8",
        )
        status = verification_status(self.root, self.catalog, self.ledger, "a-check")
        self.assertFalse(status["reusable"])
        self.assertEqual(status["execution_count"], 0)

    def test_cli_run_list_and_status_share_the_same_verdict(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli_main([
                "--root", str(self.root), "--json", "verification", "run", "a-check"
            ])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["verification"]["reusable"])
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli_main([
                "--root", str(self.root), "--json", "verification", "status", "a-check"
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["verification"]["execution_count"], 1)

    def test_catalog_rejects_duplicate_verification_ids(self) -> None:
        value = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        value["verifications"].append(dict(value["verifications"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate verification"):
            Catalog(value)


if __name__ == "__main__":
    unittest.main()
