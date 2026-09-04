from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from docgov.catalog import Catalog
from docgov.agent import MAX_MODEL_CONTEXT_CHARS, _bounded_documents, _json_from_text, govern
from docgov.cli import _init_catalog
from docgov.engine import analyze, analyze_trust, apply_safe_actions, build_snapshot, record_baseline
from docgov.git_tools import content_at_ref
from docgov.ledger import Ledger
from docgov.models import Evidence, Finding, GovernanceDecision
from docgov.supabase import inventory


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Doc Governor Test")
        catalog = {
            "version": 1,
            "taxonomy": {
                "contract": ["docs/architecture/**"],
                "state": ["docs/status/**"],
                "procedure": ["docs/operations/**"],
                "evidence": ["docs/evidence/**"],
                "decision": ["docs/decisions/**"],
            },
            "documents": [],
            "policies": {"auto_remove_new_duplicates": True, "protected": ["docs/legal/**"]},
        }
        write(self.root / ".docgov/catalog.yaml", json.dumps(catalog))
        self.catalog_path = self.root / ".docgov/catalog.yaml"
        self.ledger_path = self.root / ".docgov/ledger.jsonl"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def commit(self, message: str) -> str:
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", message], cwd=self.root, check=True)
        return git(self.root, "rev-parse", "HEAD")

    def test_new_exact_duplicate_is_removed_without_touching_canonical(self) -> None:
        canonical = "# API\n\nStatus: Current\n\nThe API contract.\n"
        write(self.root / "docs/architecture/API.md", canonical)
        self.commit("initial")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/architecture/API-notes.md", canonical)
        head = self.commit("add generated duplicate")
        snapshot = build_snapshot(self.root, self.catalog_path, base, head)
        decision = analyze(snapshot)
        self.assertEqual(decision.result, "changed")
        self.assertEqual(decision.findings[0].action, "merge_new_file")
        applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertTrue(applied.changed)
        self.assertFalse((self.root / "docs/architecture/API-notes.md").exists())
        self.assertIn(".docgov/ledger.jsonl", applied.modified_paths)
        self.assertEqual((self.root / "docs/architecture/API.md").read_text(encoding="utf-8"), canonical)
        self.assertEqual(len(Ledger(self.ledger_path).entries()), 1)

    def test_additive_duplicate_merges_content_and_rewrites_changed_links(self) -> None:
        canonical = "# API\n\nStable contract.\n"
        write(self.root / "docs/architecture/API.md", canonical)
        self.commit("initial")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/architecture/API-copy.md", canonical + "\nNew endpoint.\n")
        write(self.root / "docs/architecture/INDEX.md", "[API copy](API-copy.md)\n")
        head = self.commit("agent documentation")

        snapshot = build_snapshot(self.root, self.catalog_path, base, head)
        decision = analyze(snapshot)
        applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path)

        self.assertEqual(applied.result, "changed")
        self.assertFalse((self.root / "docs/architecture/API-copy.md").exists())
        self.assertIn("New endpoint.", (self.root / "docs/architecture/API.md").read_text(encoding="utf-8"))
        self.assertEqual(
            (self.root / "docs/architecture/INDEX.md").read_text(encoding="utf-8"),
            "[API copy](API.md)\n",
        )
        actions = [entry["action"] for entry in Ledger(self.ledger_path).entries()]
        self.assertIn("update", actions)
        self.assertIn("update_link", actions)
        self.assertIn("remove_new_duplicate", actions)

    def test_duplicate_batch_is_atomic_when_one_merge_is_unsafe(self) -> None:
        write(self.root / "docs/architecture/API.md", "# API\n")
        write(self.root / "docs/architecture/AUTH.md", "# Auth\n")
        self.commit("canonical")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/architecture/API-copy.md", "# API\n")
        write(self.root / "docs/architecture/AUTH-copy.md", "# Different\n")
        head = self.commit("duplicates")
        snapshot = build_snapshot(self.root, self.catalog_path, base, head)
        decision = GovernanceDecision(
            run_id="atomic",
            mode="review",
            result="changed",
            changed=False,
            findings=[
                Finding("duplicate", "low", "merge_new_file", ["docs/architecture/API-copy.md", "docs/architecture/API.md"], "exact"),
                Finding("duplicate", "low", "merge_new_file", ["docs/architecture/AUTH-copy.md", "docs/architecture/AUTH.md"], "unsafe"),
            ],
        )
        applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertEqual(applied.result, "action_required")
        self.assertTrue((self.root / "docs/architecture/API-copy.md").exists())
        self.assertFalse(self.ledger_path.exists())

    def test_safe_correction_is_applied_while_unrelated_finding_still_blocks(self) -> None:
        write(self.root / "docs/architecture/API.md", "# API\n")
        self.commit("canonical")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/architecture/API-copy.md", "# API\n")
        write(self.root / "docs/status/PRODUCTION.md", "# Production\n")
        head = self.commit("mixed PR")
        snapshot = build_snapshot(self.root, self.catalog_path, base, head)
        decision = GovernanceDecision(
            run_id="mixed",
            mode="review",
            result="action_required",
            changed=False,
            findings=[
                Finding("duplicate", "low", "merge_new_file", ["docs/architecture/API-copy.md", "docs/architecture/API.md"], "safe"),
                Finding("conflict", "high", "block", ["docs/status/PRODUCTION.md"], "human decision"),
            ],
        )
        applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertTrue(applied.changed)
        self.assertEqual(applied.result, "action_required")
        self.assertFalse((self.root / "docs/architecture/API-copy.md").exists())
        self.assertTrue((self.root / "docs/status/PRODUCTION.md").exists())

    def test_date_change_without_evidence_is_blocked(self) -> None:
        write(self.root / "docs/architecture/API.md", "# API\n\n最後驗證：2026-09-01\n")
        self.commit("initial")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/architecture/API.md", "# API\n\n最後驗證：2026-09-03\n")
        head = self.commit("refresh date only")
        decision = analyze(build_snapshot(self.root, self.catalog_path, base, head))
        self.assertEqual(decision.result, "action_required")
        self.assertIn("unverified_date", [item.kind for item in decision.findings])

    def test_audit_marks_expired_state_and_records_ledger(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [{
            "path": "docs/status/RELEASE.md",
            "type": "state",
            "last_verified_at": old,
        }]
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/status/RELEASE.md", "# Release\n")
        self.commit("state")
        snapshot = build_snapshot(self.root, self.catalog_path)
        decision = analyze(snapshot, mode="audit")
        self.assertEqual(decision.result, "changed")
        applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertTrue(applied.changed)
        self.assertEqual(Catalog.load(self.catalog_path).record_for("docs/status/RELEASE.md").status, "stale")
        entry = Ledger(self.ledger_path).entries()[0]
        self.assertIn("previous_hash", entry)
        self.assertIn("new_hash", entry)

    def test_yaml_timestamp_is_normalized_for_ttl_checks(self) -> None:
        write(
            self.catalog_path,
            """version: 1
documents:
  - path: docs/status/RELEASE.md
    type: state
    last_verified_at: 2026-09-03T00:00:00Z
    ttl_days: 7
""",
        )
        write(self.root / "docs/status/RELEASE.md", "# Release\n")
        self.commit("state with YAML timestamp")

        record = Catalog.load(self.catalog_path).record_for("docs/status/RELEASE.md")
        self.assertIsInstance(record.last_verified_at, str)
        decision = analyze_trust(
            build_snapshot(self.root, self.catalog_path),
            self.ledger_path,
            now=datetime(2026, 9, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(decision.result, "action_required")
        self.assertNotIn("passed its 7-day", " ".join(item.reason for item in decision.findings))

    def test_binary_dependency_at_source_ref_has_stable_content(self) -> None:
        binary_path = self.root / "assets/image.png"
        binary_path.parent.mkdir(parents=True, exist_ok=True)
        binary_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
        head = self.commit("binary dependency")

        self.assertEqual(content_at_ref(self.root, head, "assets/image.png"), binary_path.read_bytes().hex())

    def test_conflicting_canonical_documents_are_not_deleted(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [
            {"path": "docs/architecture/API.md", "type": "contract", "authority": "canonical", "canonical_key": "api"},
            {"path": "docs/architecture/API-v2.md", "type": "contract", "authority": "canonical", "canonical_key": "api"},
        ]
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/architecture/API.md", "# API one\n")
        write(self.root / "docs/architecture/API-v2.md", "# API two\n")
        self.commit("conflict")
        decision = analyze(build_snapshot(self.root, self.catalog_path), mode="audit")
        self.assertEqual(decision.result, "action_required")
        self.assertIn("conflict", [item.kind for item in decision.findings])
        self.assertTrue((self.root / "docs/architecture/API.md").exists())
        self.assertTrue((self.root / "docs/architecture/API-v2.md").exists())

    def test_supabase_inventory_reads_functions_and_jwt_flags(self) -> None:
        write(self.root / "supabase/config.toml", """
[functions.health-check]
verify_jwt = true

[functions.cron-task]
verify_jwt = false
""")
        write(self.root / "supabase/functions/health-check/index.ts", "export default {};\n")
        write(self.root / "supabase/functions/cron-task/index.ts", "export default {};\n")
        write(self.root / "supabase/migrations/001.sql", """
create or replace function public.profile_summary(user_id uuid)
returns json language sql as $$ select '{}'::json $$;
insert into storage.buckets (id) values ('post-media');
create policy media_read on storage.objects using (bucket_id = 'avatars');
""")
        write(self.root / "docs/architecture/API.md", '<!-- docgov:supabase-inventory {"functions":["health-check"]} -->\n')
        result = inventory(self.root)
        self.assertEqual(result["source_functions"], ["cron-task", "health-check"])
        self.assertEqual(result["config_functions"], ["cron-task", "health-check"])
        self.assertEqual(result["jwt_flags"], {"cron-task": False, "health-check": True})
        self.assertEqual(result["rpc_functions"], ["profile_summary"])
        self.assertEqual(result["storage_buckets"], ["avatars", "post-media"])
        self.assertEqual(result["document_markers"]["docs/architecture/API.md"]["functions"], ["health-check"])

    def test_supabase_source_config_mismatch_is_blocked(self) -> None:
        write(self.root / "supabase/config.toml", "[functions.health-check]\nverify_jwt = true\n")
        write(self.root / "supabase/functions/health-check/index.ts", "export default {};\n")
        write(self.root / "supabase/functions/undeclared/index.ts", "export default {};\n")
        self.commit("mismatched function inventory")
        decision = analyze(build_snapshot(self.root, self.catalog_path), mode="verify")
        self.assertEqual(decision.result, "action_required")
        self.assertTrue(any(item.kind == "conflict" for item in decision.findings))

    def test_supabase_edge_function_updates_governed_inventory_markers(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [
            {
                "path": "docs/architecture/API.md",
                "type": "contract",
                "status": "current",
                "depends_on": ["supabase/functions/**", "supabase/config.toml"],
            },
            {
                "path": "docs/architecture/EDGE_FUNCTIONS.md",
                "type": "contract",
                "status": "current",
                "depends_on": ["supabase/functions/**", "supabase/config.toml"],
            },
        ]
        write(self.catalog_path, json.dumps(catalog))
        marker = '<!-- docgov:supabase-inventory {"functions":["health-check"],"jwt_flags":{"health-check":true}} -->\n'
        write(self.root / "docs/architecture/API.md", "# API\n\n" + marker)
        write(self.root / "docs/architecture/EDGE_FUNCTIONS.md", "# Edge\n\n" + marker)
        write(self.root / "supabase/functions/health-check/index.ts", "export default {};\n")
        write(self.root / "supabase/config.toml", "[functions.health-check]\nverify_jwt = true\n")
        self.commit("initial inventory")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "supabase/functions/send-email/index.ts", "export default {};\n")
        write(
            self.root / "supabase/config.toml",
            "[functions.health-check]\nverify_jwt = true\n\n[functions.send-email]\nverify_jwt = false\n",
        )
        head = self.commit("add edge function")

        snapshot = build_snapshot(self.root, self.catalog_path, base, head, ledger_path=self.ledger_path)
        decision = analyze(snapshot)
        self.assertEqual(
            {finding.documents[0] for finding in decision.findings if finding.action == "update"},
            {"docs/architecture/API.md", "docs/architecture/EDGE_FUNCTIONS.md"},
        )
        applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertEqual(applied.result, "changed")
        for document in ("docs/architecture/API.md", "docs/architecture/EDGE_FUNCTIONS.md"):
            content = (self.root / document).read_text(encoding="utf-8")
            self.assertIn('"functions":["health-check","send-email"]', content)
            self.assertIn('"send-email":false', content)
            baseline = Ledger(self.ledger_path).latest_for(document, "verify_current")
            self.assertEqual(baseline["verifier"], "supabase_inventory")

    def test_nested_supabase_inventory_reports_relative_config_path(self) -> None:
        write(self.root / "examples/demo/supabase/config.toml", "[functions.health-check]\nverify_jwt = true\n")
        write(self.root / "examples/demo/supabase/functions/health-check/index.ts", "export default {};\n")
        result = inventory(self.root)
        self.assertEqual(result["config_path"], "examples/demo/supabase/config.toml")
        self.assertEqual(result["source_functions"], ["health-check"])

    def test_similar_but_nonidentical_duplicate_requires_human(self) -> None:
        write(self.root / "docs/architecture/API.md", "# API\n\nThe API contract covers health checks and profiles.\n")
        self.commit("initial")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/architecture/API-notes.md", "# API\n\nThe API contract covers health checks and profile endpoints.\n")
        head = self.commit("add near duplicate")
        snapshot = build_snapshot(self.root, self.catalog_path, base, head)
        decision = analyze(snapshot)
        self.assertEqual(decision.findings[0].action, "merge_new_file")
        applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertEqual(applied.result, "action_required")
        self.assertTrue((self.root / "docs/architecture/API-notes.md").exists())

    def test_ledger_append_is_idempotent(self) -> None:
        ledger = Ledger(self.ledger_path)
        evidence = [Evidence(path="supabase/config.toml", sha256="abc")]
        self.assertTrue(ledger.append(run_id="run", document="docs/API.md", action="mark_stale", reason="test", evidence=evidence))
        self.assertFalse(ledger.append(run_id="run", document="docs/API.md", action="mark_stale", reason="test", evidence=evidence))
        self.assertEqual(len(ledger.entries()), 1)

    def test_unique_decision_document_is_preserved(self) -> None:
        self.commit("initial")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/decisions/001-use-bedrock.md", "# Use Bedrock\n\nDecision record.\n")
        head = self.commit("add decision")
        decision = analyze(build_snapshot(self.root, self.catalog_path, base, head))
        self.assertEqual(decision.result, "pass")
        self.assertTrue((self.root / "docs/decisions/001-use-bedrock.md").exists())
        applied = apply_safe_actions(
            build_snapshot(self.root, self.catalog_path, base, head),
            decision,
            self.catalog_path,
            self.ledger_path,
        )
        self.assertTrue(applied.changed)
        self.assertEqual(Catalog.load(self.catalog_path).record_for("docs/decisions/001-use-bedrock.md").type, "decision")

    def test_existing_evidence_is_immutable(self) -> None:
        write(self.root / "docs/evidence/2026-09-01/run.json", '{"ok":true}\n')
        self.commit("evidence")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/evidence/2026-09-01/run.json", '{"ok":false}\n')
        head = self.commit("rewrite evidence")
        decision = analyze(build_snapshot(self.root, self.catalog_path, base, head))
        self.assertEqual(decision.result, "action_required")
        self.assertTrue(any(item.kind == "conflict" for item in decision.findings))
        applied = apply_safe_actions(build_snapshot(self.root, self.catalog_path, base, head), decision, self.catalog_path, self.ledger_path)
        self.assertFalse(applied.changed)

    def test_catalog_verification_date_without_evidence_is_blocked(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [{
            "path": "docs/status/RELEASE.md",
            "type": "state",
            "last_verified_at": "2026-09-01T00:00:00Z",
        }]
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/status/RELEASE.md", "# Release\n")
        self.commit("state")
        base = git(self.root, "rev-parse", "HEAD")
        catalog["documents"][0]["last_verified_at"] = "2026-09-03T00:00:00Z"
        write(self.catalog_path, json.dumps(catalog))
        head = self.commit("refresh catalog date only")
        decision = analyze(build_snapshot(self.root, self.catalog_path, base, head))
        self.assertEqual(decision.result, "action_required")
        self.assertIn("unverified_date", [item.kind for item in decision.findings])

    def test_production_status_change_is_protected(self) -> None:
        write(self.root / "docs/status/PRODUCTION.md", "# Production\n\nStatus: ready\n")
        self.commit("production status")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/status/PRODUCTION.md", "# Production\n\nStatus: incident\n")
        head = self.commit("change production status")
        decision = analyze(build_snapshot(self.root, self.catalog_path, base, head))
        self.assertEqual(decision.result, "action_required")
        self.assertTrue(any(item.kind == "conflict" for item in decision.findings))

    def test_approved_protected_edit_is_acknowledged_without_rewrite(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["policies"]["protected"] = ["docs/public/**"]
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/public/COPY.md", "# Public copy\n\nold\n")
        self.commit("public copy")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/public/COPY.md", "# Public copy\n\nreviewed update\n")
        head = self.commit("review public copy")
        snapshot = build_snapshot(self.root, self.catalog_path, base, head)
        decision = analyze(snapshot)
        applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path, approved=True)
        self.assertEqual(applied.result, "pass")
        self.assertFalse(applied.changed)
        self.assertIn("reviewed update", (self.root / "docs/public/COPY.md").read_text(encoding="utf-8"))

    def test_governor_removal_commit_does_not_reopen_duplicate_as_orphan(self) -> None:
        canonical = "# API\n\nThe API contract.\n"
        write(self.root / "docs/architecture/API.md", canonical)
        self.commit("initial")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/architecture/API-copy.md", canonical)
        head = self.commit("add duplicate")
        snapshot = build_snapshot(self.root, self.catalog_path, base, head)
        decision = analyze(snapshot)
        apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "chore docs"], cwd=self.root, check=True)
        new_head = git(self.root, "rev-parse", "HEAD")
        rerun = analyze(build_snapshot(self.root, self.catalog_path, head, new_head, self.ledger_path))
        self.assertEqual(rerun.result, "pass")

    def test_date_change_with_evidence_is_not_blocked(self) -> None:
        write(self.root / "docs/architecture/API.md", "# API\n\n最後驗證：2026-09-01\n")
        self.commit("initial")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/architecture/API.md", "# API\n\n最後驗證：2026-09-03\n")
        write(self.root / "docs/evidence/2026-09-03/run.json", '{"verified":true}\n')
        head = self.commit("refresh with evidence")
        decision = analyze(build_snapshot(self.root, self.catalog_path, base, head))
        self.assertNotIn("unverified_date", [item.kind for item in decision.findings])

    def test_protected_duplicate_requires_human(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["policies"]["protected"] = ["docs/architecture/**"]
        write(self.catalog_path, json.dumps(catalog))
        canonical = "# Protected API\n\nThe canonical contract.\n"
        write(self.root / "docs/architecture/API.md", canonical)
        self.commit("initial")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/architecture/API-copy.md", canonical)
        head = self.commit("add protected duplicate")
        decision = analyze(build_snapshot(self.root, self.catalog_path, base, head))
        self.assertEqual(decision.result, "action_required")
        self.assertTrue(any(item.kind == "duplicate" and item.risk == "high" for item in decision.findings))
        self.assertTrue((self.root / "docs/architecture/API-copy.md").exists())

    def test_approved_label_allows_exact_protected_duplicate_once(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["policies"]["protected"] = ["docs/architecture/**"]
        write(self.catalog_path, json.dumps(catalog))
        canonical = "# Protected API\n\nThe canonical contract.\n"
        write(self.root / "docs/architecture/API.md", canonical)
        self.commit("initial")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "docs/architecture/API-copy.md", canonical)
        head = self.commit("add protected duplicate")
        snapshot = build_snapshot(self.root, self.catalog_path, base, head)
        decision = analyze(snapshot)
        applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path, approved=True)
        self.assertTrue(applied.changed)
        self.assertFalse((self.root / "docs/architecture/API-copy.md").exists())
        self.assertEqual(applied.result, "changed")

    def test_unknown_document_location_is_blocked(self) -> None:
        self.commit("initial")
        base = git(self.root, "rev-parse", "HEAD")
        write(self.root / "notes.md", "# Unclassified\n")
        head = self.commit("add unclassified note")
        decision = analyze(build_snapshot(self.root, self.catalog_path, base, head))
        self.assertEqual(decision.result, "action_required")
        self.assertIn("misclassified", [item.kind for item in decision.findings])

    def test_catalog_rejects_a_sixth_core_type(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["taxonomy"]["note"] = ["notes/**"]
        write(self.catalog_path, json.dumps(catalog))
        with self.assertRaisesRegex(ValueError, "Unsupported document type"):
            Catalog.load(self.catalog_path)

    def test_init_proposes_every_tracked_markdown_as_review_required(self) -> None:
        write(self.root / "README.md", "# Root\n")
        write(self.root / "docs/operations/RUNBOOK.md", "# Runbook\n")
        write(self.root / "loose-notes.md", "# Notes\n")
        self.commit("existing docs")
        proposal = _init_catalog(self.root, self.catalog_path)
        records = {item["path"]: item for item in proposal["documents"]}
        self.assertEqual(set(records), {"README.md", "docs/operations/RUNBOOK.md", "loose-notes.md"})
        self.assertEqual(records["README.md"]["type"], "contract")
        self.assertEqual(records["docs/operations/RUNBOOK.md"]["type"], "procedure")
        self.assertTrue(all(item["status"] == "review_required" for item in records.values()))

    def test_invalid_model_payload_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _json_from_text('{"findings": "not-an-array"}')

    def test_model_failure_is_blocked_without_mutation(self) -> None:
        write(self.root / "docs/architecture/API.md", "# API\n")
        self.commit("canonical")
        snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        with patch("docgov.agent.invoke_strands", side_effect=TimeoutError("timed out")):
            decision = govern(snapshot, mode="review", enable_model=True)
        self.assertEqual(decision.result, "blocked")
        self.assertFalse(decision.changed)
        self.assertIn("failed closed", decision.error)
        self.assertFalse(self.ledger_path.exists())

    def test_model_receives_only_bounded_relevant_markdown(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [
            {"path": "docs/architecture/API.md", "type": "contract"},
            {"path": "docs/architecture/OTHER.md", "type": "contract"},
            {"path": "docs/status/RELEASE.md", "type": "state"},
        ]
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/architecture/API.md", "A" * (MAX_MODEL_CONTEXT_CHARS * 2))
        write(self.root / "docs/architecture/OTHER.md", "# Related\n")
        write(self.root / "docs/status/RELEASE.md", "# Unrelated\n")
        base = self.commit("documents")
        write(self.root / "docs/architecture/API.md", "B" * (MAX_MODEL_CONTEXT_CHARS * 2))
        head = self.commit("change API")
        documents = _bounded_documents(build_snapshot(self.root, self.catalog_path, base, head))
        self.assertIn("docs/architecture/API.md", documents)
        self.assertNotIn("docs/status/RELEASE.md", documents)
        self.assertLessEqual(sum(len(value) for value in documents.values()), MAX_MODEL_CONTEXT_CHARS)

    def test_model_finding_cannot_remove_outside_new_markdown_boundary(self) -> None:
        write(self.root / "docs/architecture/API.md", "# API\n")
        self.commit("canonical")
        snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        decision = GovernanceDecision(
            run_id="malicious-model",
            mode="review",
            result="changed",
            changed=False,
            findings=[Finding(
                kind="duplicate",
                risk="low",
                action="merge_new_file",
                documents=["../../outside.md", "docs/architecture/API.md"],
                reason="model proposal",
            )],
        )
        applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertEqual(applied.result, "action_required")
        self.assertEqual(applied.findings[0].action, "block")

    def test_strict_verify_rejects_unregistered_tracked_markdown(self) -> None:
        write(self.root / "notes.md", "# Unregistered\n")
        self.commit("add unregistered markdown")
        snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        decision = analyze_trust(snapshot, self.ledger_path)
        self.assertEqual(decision.result, "action_required")
        self.assertEqual(decision.trust_results[0].scope, "untrusted")
        self.assertIn("misclassified", [finding.kind for finding in decision.findings])

    def test_strict_verify_quarantines_declared_stale_until_requested(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [{
            "path": "docs/status/OLD.md",
            "type": "state",
            "status": "stale",
        }]
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/status/OLD.md", "# Old state\n")
        self.commit("quarantined state")
        snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        global_decision = analyze_trust(snapshot, self.ledger_path)
        requested_decision = analyze_trust(snapshot, self.ledger_path, ["docs/status/OLD.md"])
        self.assertEqual(global_decision.result, "pass")
        self.assertEqual(global_decision.trust_results[0].scope, "untrusted")
        self.assertEqual(requested_decision.result, "action_required")

    def test_strict_verify_detects_dependency_fingerprint_drift(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [{
            "path": "docs/architecture/API.md",
            "type": "contract",
            "status": "current",
            "depends_on": ["src/**"],
        }]
        catalog["policies"]["require_verification_ledger"] = True
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/architecture/API.md", "# API\n")
        write(self.root / "src/api.txt", "v1\n")
        self.commit("verified contract")
        snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        baseline = record_baseline(snapshot, self.ledger_path, approved=True)
        self.assertEqual(baseline.result, "changed")
        self.assertEqual(analyze_trust(snapshot, self.ledger_path).result, "pass")
        ledger_entry = Ledger(self.ledger_path).latest_for("docs/architecture/API.md", "verify_current")
        self.assertEqual(len(ledger_entry["evidence"]), 1)
        self.assertEqual(ledger_entry["evidence"][0]["path"], "src/**")
        self.assertEqual(ledger_entry["evidence"][0]["detail"], "1 file(s)")

        write(self.root / "src/api.txt", "v2\n")
        drifted = analyze_trust(
            build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path),
            self.ledger_path,
        )
        self.assertEqual(drifted.result, "action_required")
        self.assertIn("dependency changed", drifted.findings[0].reason.lower())

    def test_strict_verify_detects_new_untracked_dependency(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [{
            "path": "docs/architecture/API.md",
            "type": "contract",
            "status": "current",
            "depends_on": ["src/**"],
        }]
        catalog["policies"]["require_verification_ledger"] = True
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/architecture/API.md", "# API\n")
        write(self.root / "src/api.txt", "v1\n")
        self.commit("verified contract")
        snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        record_baseline(snapshot, self.ledger_path, approved=True)
        write(self.root / "src/new-endpoint.txt", "new\n")

        decision = analyze_trust(
            build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path),
            self.ledger_path,
        )
        self.assertEqual(decision.result, "action_required")
        self.assertIn("dependency changed", decision.findings[0].reason.lower())

    def test_strict_verify_applies_type_scoped_trust(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [
            {"path": "docs/evidence/2026-09-03/run.md", "type": "evidence", "status": "immutable"},
            {"path": "docs/decisions/001.md", "type": "decision", "status": "accepted"},
        ]
        catalog["policies"]["require_verification_ledger"] = True
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/evidence/2026-09-03/run.md", "# Historical run\n")
        write(self.root / "docs/decisions/001.md", "# Decision\n")
        self.commit("scoped records")
        snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        record_baseline(snapshot, self.ledger_path, approved=True)
        decision = analyze_trust(snapshot, self.ledger_path)
        scopes = {result.path: result.scope for result in decision.trust_results}
        self.assertEqual(scopes["docs/evidence/2026-09-03/run.md"], "historical_evidence")
        self.assertEqual(scopes["docs/decisions/001.md"], "rationale_only")

    def test_strict_verify_rejects_expired_state_even_with_baseline(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [{
            "path": "docs/status/RELEASE.md",
            "type": "state",
            "status": "current",
            "last_verified_at": old,
            "depends_on": ["src/release.txt"],
        }]
        catalog["policies"]["require_verification_ledger"] = True
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/status/RELEASE.md", "# Release\n")
        write(self.root / "src/release.txt", "verified\n")
        self.commit("expired state")
        snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        record_baseline(snapshot, self.ledger_path, approved=True)
        decision = analyze_trust(snapshot, self.ledger_path)
        self.assertEqual(decision.result, "action_required")
        self.assertIn("verification window", decision.findings[0].reason)

    def test_baseline_rejects_state_without_evidence_and_writes_nothing(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [{
            "path": "docs/status/RELEASE.md",
            "type": "state",
            "status": "current",
            "last_verified_at": datetime.now(timezone.utc).isoformat(),
        }]
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/status/RELEASE.md", "# Release\n")
        self.commit("state without evidence")
        decision = record_baseline(
            build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path),
            self.ledger_path,
            approved=True,
        )
        self.assertEqual(decision.result, "action_required")
        self.assertFalse(self.ledger_path.exists())

    def test_strict_verify_ignores_untracked_markdown(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [{
            "path": "docs/architecture/API.md",
            "type": "contract",
            "status": "current",
        }]
        catalog["policies"]["require_verification_ledger"] = True
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/architecture/API.md", "# API\n")
        self.commit("tracked docs")
        write(self.root / "generated.md", "# Generated\n")
        snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        record_baseline(snapshot, self.ledger_path, approved=True)
        decision = analyze_trust(snapshot, self.ledger_path)
        self.assertEqual(decision.result, "pass")
        self.assertNotIn("generated.md", [result.path for result in decision.trust_results])

    def test_strict_verify_can_read_a_canonical_git_ref(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"] = [{
            "path": "docs/architecture/API.md",
            "type": "contract",
            "status": "current",
        }]
        catalog["policies"]["require_verification_ledger"] = True
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/architecture/API.md", "# Canonical API\n")
        self.commit("canonical docs")
        canonical_snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        record_baseline(canonical_snapshot, self.ledger_path, approved=True)
        canonical_sha = self.commit("canonical verification ledger")
        subprocess.run(["git", "checkout", "-qb", "candidate"], cwd=self.root, check=True)
        write(self.root / "docs/architecture/API.md", "# Stale candidate API\n")
        candidate_catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        candidate_catalog["documents"] = []
        write(self.catalog_path, json.dumps(candidate_catalog))
        self.commit("stale candidate docs")

        candidate = analyze_trust(
            build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path),
            self.ledger_path,
        )
        canonical = analyze_trust(
            build_snapshot(
                self.root,
                self.catalog_path,
                ledger_path=self.ledger_path,
                source_ref=canonical_sha,
            ),
            self.ledger_path,
        )
        self.assertEqual(candidate.result, "action_required")
        self.assertEqual(canonical.result, "pass")


if __name__ == "__main__":
    unittest.main()
