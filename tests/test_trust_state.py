from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docgov.cli import main as cli_main
from docgov.engine import apply_safe_actions, analyze, build_snapshot, record_baseline
from docgov.ledger import Ledger
from docgov.models import Evidence, Finding, GovernanceDecision
from docgov.trust_state import (
    DEFAULT_TRUST_STATE_PATH,
    TRUST_STATE_VERSION,
    TrustStateError,
    build_trust_state,
    load_trust_state,
    record_depends_on_environment,
    render_trust_state,
    trust_entries,
    unresolved_drift_environments,
    write_trust_state,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


class TrustStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Doc Governor Test")
        self.catalog_path = self.root / ".docgov/catalog.yaml"
        self.ledger_path = self.root / ".docgov/ledger.jsonl"
        self.trust_state_path = self.root / DEFAULT_TRUST_STATE_PATH

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_catalog(self, documents: list[dict], policies: dict | None = None) -> None:
        write(self.catalog_path, json.dumps({
            "version": 1,
            "taxonomy": {
                "contract": ["docs/architecture/**"],
                "state": ["docs/status/**"],
                "procedure": ["docs/operations/**"],
                "evidence": ["docs/evidence/**"],
                "decision": ["docs/decisions/**"],
            },
            "documents": documents,
            "policies": policies if policies is not None else {"auto_remove_new_duplicates": True},
        }))

    def commit(self, message: str) -> str:
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", message], cwd=self.root, check=True)
        return git(self.root, "rev-parse", "HEAD")

    def snapshot(self, base: str | None = None, head: str | None = None):
        return build_snapshot(self.root, self.catalog_path, base, head, ledger_path=self.ledger_path)

    def empty_decision(self) -> GovernanceDecision:
        return GovernanceDecision(run_id="test", mode="review", result="pass", changed=False)

    def state_for(self, decision: GovernanceDecision | None = None) -> dict:
        return build_trust_state(
            decision or self.empty_decision(),
            self.snapshot(),
            ledger_path=self.ledger_path,
        )

    # ---- the binary the MCP layer exposes -------------------------------

    def test_document_without_a_ledger_baseline_is_not_usable(self) -> None:
        self.write_catalog([{
            "path": "docs/architecture/API.md",
            "type": "contract",
            "status": "current",
            "depends_on": ["src/**"],
        }])
        write(self.root / "docs/architecture/API.md", "# API\n")
        write(self.root / "src/api.ts", "export const api = 1;\n")
        self.commit("initial")

        entries = trust_entries(self.state_for())
        entry = entries["docs/architecture/API.md"]
        self.assertFalse(entry.usable)
        self.assertIn("ledger", entry.reason)
        self.assertEqual(entry.source_pointers, ["src/api.ts"])

    def test_verified_document_is_usable_and_records_its_verification(self) -> None:
        self.write_catalog([{
            "path": "docs/architecture/API.md",
            "type": "contract",
            "status": "current",
            "depends_on": ["src/**"],
        }])
        write(self.root / "docs/architecture/API.md", "# API\n")
        write(self.root / "src/api.ts", "export const api = 1;\n")
        head = self.commit("initial")
        record_baseline(self.snapshot(), self.ledger_path, approved=True)

        entry = trust_entries(self.state_for())["docs/architecture/API.md"]
        self.assertTrue(entry.usable)
        self.assertEqual(entry.scope, "current_fact")
        self.assertTrue(entry.dependency_fingerprint)
        # The recorded SHA describes the verification, not the serializing run.
        self.assertEqual(entry.head_sha, head)
        self.assertIsNotNone(entry.verified_at)

    def test_changed_dependency_makes_a_verified_document_unusable(self) -> None:
        self.write_catalog([{
            "path": "docs/architecture/API.md",
            "type": "contract",
            "status": "current",
            "depends_on": ["src/**"],
        }])
        write(self.root / "docs/architecture/API.md", "# API\n")
        write(self.root / "src/api.ts", "export const api = 1;\n")
        self.commit("initial")
        record_baseline(self.snapshot(), self.ledger_path, approved=True)
        write(self.root / "src/api.ts", "export const api = 2;\n")

        entry = trust_entries(self.state_for())["docs/architecture/API.md"]
        self.assertFalse(entry.usable)
        self.assertEqual(entry.scope, "untrusted")

    def test_expired_state_document_is_not_usable(self) -> None:
        stale = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat().replace("+00:00", "Z")
        self.write_catalog([{
            "path": "docs/status/RELEASE.md",
            "type": "state",
            "status": "current",
            "last_verified_at": stale,
        }])
        write(self.root / "docs/status/RELEASE.md", "# Release\n")
        self.commit("initial")
        record_baseline(self.snapshot(), self.ledger_path, approved=True)

        entry = trust_entries(self.state_for())["docs/status/RELEASE.md"]
        self.assertFalse(entry.usable)

    def test_review_required_record_is_not_usable(self) -> None:
        self.write_catalog([{
            "path": "docs/architecture/API.md",
            "type": "contract",
            "status": "review_required",
        }])
        write(self.root / "docs/architecture/API.md", "# API\n")
        self.commit("initial")

        entry = trust_entries(self.state_for())["docs/architecture/API.md"]
        self.assertFalse(entry.usable)

    def test_evidence_document_keeps_its_internal_scope_while_staying_usable(self) -> None:
        self.write_catalog([{
            "path": "docs/evidence/2026-09-01-scan.md",
            "type": "evidence",
            "status": "current",
        }])
        write(self.root / "docs/evidence/2026-09-01-scan.md", "# Scan\n")
        self.commit("initial")
        record_baseline(self.snapshot(), self.ledger_path, approved=True)

        entry = trust_entries(self.state_for())["docs/evidence/2026-09-01-scan.md"]
        self.assertTrue(entry.usable)
        self.assertEqual(entry.scope, "historical_evidence")

    # ---- pointers to a trustworthy alternative (D2, D6) -----------------

    def test_merged_duplicate_keeps_a_tombstone_pointing_at_the_canonical_file(self) -> None:
        self.write_catalog([])
        canonical = "# API\n\nThe API contract.\n"
        write(self.root / "docs/architecture/API.md", canonical)
        base = self.commit("initial")
        write(self.root / "docs/architecture/API-notes.md", canonical)
        head = self.commit("duplicate")

        snapshot = self.snapshot(base, head)
        decision = apply_safe_actions(
            snapshot, analyze(snapshot), self.catalog_path, self.ledger_path
        )
        self.assertFalse((self.root / "docs/architecture/API-notes.md").exists())

        entries = trust_entries(self.state_for(decision))
        tombstone = entries["docs/architecture/API-notes.md"]
        self.assertFalse(tombstone.usable)
        self.assertEqual(tombstone.canonical_path, "docs/architecture/API.md")

    def test_non_canonical_record_points_at_its_canonical_peer(self) -> None:
        self.write_catalog([
            {
                "path": "docs/architecture/API.md",
                "type": "contract",
                "status": "current",
                "authority": "canonical",
                "canonical_key": "public-api",
            },
            {
                "path": "docs/architecture/API-LEGACY.md",
                "type": "contract",
                "status": "superseded",
                "authority": "superseded",
                "canonical_key": "public-api",
            },
        ])
        write(self.root / "docs/architecture/API.md", "# API\n")
        write(self.root / "docs/architecture/API-LEGACY.md", "# Legacy API\n")
        self.commit("initial")

        entry = trust_entries(self.state_for())["docs/architecture/API-LEGACY.md"]
        self.assertFalse(entry.usable)
        self.assertEqual(entry.canonical_path, "docs/architecture/API.md")

    def test_unusable_documents_are_listed_rather_than_hidden(self) -> None:
        self.write_catalog([
            {"path": "docs/architecture/API.md", "type": "contract", "status": "stale"},
        ])
        write(self.root / "docs/architecture/API.md", "# API\n")
        self.commit("initial")

        state = self.state_for()
        self.assertIn("docs/architecture/API.md", trust_entries(state))

    # ---- environment drift (WP4 consequence) ---------------------------

    def test_drifted_environment_blocks_the_state_documents_that_describe_it(self) -> None:
        self.write_catalog([{
            "path": "docs/status/RELEASE.md",
            "type": "state",
            "status": "current",
            "last_verified_at": _now(),
            "depends_on": ["ops/production/**"],
            "environments": ["production"],
        }])
        write(self.root / "docs/status/RELEASE.md", "# Release\n")
        write(self.root / "ops/production/inventory.json", "{}\n")
        self.commit("initial")
        record_baseline(self.snapshot(), self.ledger_path, approved=True)
        self.assertTrue(trust_entries(self.state_for())["docs/status/RELEASE.md"].usable)

        Ledger(self.ledger_path).append(
            run_id="drift-run",
            document="supabase:production",
            action="environment_drift",
            reason="Production diverged from the last promoted release.",
        )
        entry = trust_entries(self.state_for())["docs/status/RELEASE.md"]
        self.assertFalse(entry.usable)
        self.assertIn("production", entry.reason)
        self.assertEqual(self.state_for()["drifted_environments"], ["production"])

    def test_cleared_drift_restores_the_dependent_documents(self) -> None:
        ledger = Ledger(self.ledger_path)
        ledger.append(run_id="a", document="supabase:production", action="environment_drift", reason="drift")
        ledger.append(run_id="b", document="supabase:production", action="environment_drift_cleared", reason="clear")
        self.assertEqual(unresolved_drift_environments(ledger.entries()), set())

    def test_environment_dependency_is_declared_explicitly_or_by_path_segment(self) -> None:
        from docgov.models import DocumentRecord

        explicit = DocumentRecord(path="a.md", type="state", environments=["production"])
        by_path = DocumentRecord(
            path="b.md",
            type="state",
            depends_on=["docs/security-evidence/supabase-advisors/production/**"],
        )
        unrelated = DocumentRecord(path="c.md", type="state", depends_on=["src/**"])
        self.assertTrue(record_depends_on_environment(explicit, "production"))
        self.assertTrue(record_depends_on_environment(by_path, "production"))
        self.assertFalse(record_depends_on_environment(unrelated, "production"))

    def test_drift_finding_in_the_current_decision_blocks_immediately(self) -> None:
        self.write_catalog([{
            "path": "docs/status/RELEASE.md",
            "type": "state",
            "status": "current",
            "last_verified_at": _now(),
            "depends_on": ["ops/production/**"],
            "environments": ["production"],
        }])
        write(self.root / "docs/status/RELEASE.md", "# Release\n")
        write(self.root / "ops/production/inventory.json", "{}\n")
        self.commit("initial")
        record_baseline(self.snapshot(), self.ledger_path, approved=True)

        decision = self.empty_decision()
        decision.findings.append(Finding(
            kind="environment_drift",
            risk="high",
            action="block",
            documents=[],
            reason="Production drifted.",
            evidence=[Evidence(path="production", kind="environment")],
        ))
        self.assertFalse(trust_entries(self.state_for(decision))["docs/status/RELEASE.md"].usable)

    # ---- file format ----------------------------------------------------

    def test_write_trust_state_reports_no_change_for_an_identical_tree(self) -> None:
        self.write_catalog([{"path": "docs/architecture/API.md", "type": "contract", "status": "current"}])
        write(self.root / "docs/architecture/API.md", "# API\n")
        self.commit("initial")

        state = self.state_for()
        self.assertTrue(write_trust_state(self.trust_state_path, state))
        first = self.trust_state_path.read_text(encoding="utf-8")
        self.assertFalse(write_trust_state(self.trust_state_path, self.state_for()))
        self.assertEqual(self.trust_state_path.read_text(encoding="utf-8"), first)

    def test_entries_are_sorted_by_path_with_sorted_keys(self) -> None:
        self.write_catalog([])
        write(self.root / "docs/architecture/Z.md", "# Z\n")
        write(self.root / "docs/architecture/A.md", "# A\n")
        self.commit("initial")

        state = self.state_for()
        paths = [item["path"] for item in state["documents"]]
        self.assertEqual(paths, sorted(paths))
        rendered = render_trust_state(state)
        self.assertLess(rendered.index('"catalog_path"'), rendered.index('"documents"'))
        self.assertTrue(rendered.endswith("\n"))

    def test_load_trust_state_rejects_an_unknown_version(self) -> None:
        write(self.trust_state_path, json.dumps({"version": TRUST_STATE_VERSION + 1, "documents": []}))
        with self.assertRaises(TrustStateError):
            load_trust_state(self.trust_state_path)

    def test_load_trust_state_rejects_a_missing_file(self) -> None:
        with self.assertRaises(TrustStateError):
            load_trust_state(self.trust_state_path)

    def test_load_trust_state_rejects_a_file_without_documents(self) -> None:
        write(self.trust_state_path, json.dumps({"version": TRUST_STATE_VERSION}))
        with self.assertRaises(TrustStateError):
            load_trust_state(self.trust_state_path)

    # ---- CLI wiring ------------------------------------------------------

    def test_review_apply_writes_and_commits_the_trust_state(self) -> None:
        self.write_catalog([{"path": "docs/architecture/API.md", "type": "contract", "status": "current"}])
        write(self.root / "docs/architecture/API.md", "# API\n")
        self.commit("initial")

        exit_code = cli_main(["--root", str(self.root), "review", "--apply"])
        self.assertIn(exit_code, {0, 2})
        self.assertTrue(self.trust_state_path.exists())
        state = load_trust_state(self.trust_state_path)
        self.assertEqual(state["version"], TRUST_STATE_VERSION)
        self.assertIn("docs/architecture/API.md", trust_entries(state))

    def test_trust_state_flag_redirects_the_output(self) -> None:
        self.write_catalog([{"path": "docs/architecture/API.md", "type": "contract", "status": "current"}])
        write(self.root / "docs/architecture/API.md", "# API\n")
        self.commit("initial")

        cli_main([
            "--root", str(self.root),
            "--trust-state", ".docgov/other-trust.json",
            "audit", "--apply",
        ])
        self.assertTrue((self.root / ".docgov/other-trust.json").exists())
        self.assertFalse(self.trust_state_path.exists())


if __name__ == "__main__":
    unittest.main()
