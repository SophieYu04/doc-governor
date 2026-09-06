from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from docgov.cli import main as cli_main
from docgov.engine import build_snapshot, record_baseline
from docgov.ledger import Ledger
from docgov.mcp_server import DocumentSupply, SupplyConfig
from docgov.supabase_remote import (
    PROMOTION_ACTION,
    SupabaseAdvisorError,
    build_evidence_snapshot,
    collect_advisor_evidence,
    compare_environments,
    environment_fingerprint,
    fetch_advisors,
    parse_projects,
    write_evidence,
)
from docgov.trust_state import DEFAULT_TRUST_STATE_PATH, load_trust_state, trust_entries


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def lint(name: str, level: str, entity: str, observed_at: str = "2026-09-05T00:00:00Z") -> dict:
    return {
        "name": name,
        "level": level,
        "facing": "EXTERNAL",
        "categories": ["SECURITY"],
        "description": f"private description for {entity}",
        "detail": f"private detail for {entity}",
        "metadata": {"schema": "private", "name": entity},
        "cache_key": f"{name}_private_{entity}",
        "observed_at": observed_at,
    }


class FakeResponse:
    def __init__(self, value: dict):
        self.value = value

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.value).encode("utf-8")


class SupabaseRemoteTests(unittest.TestCase):
    def test_project_parser_requires_unique_named_refs(self) -> None:
        self.assertEqual(
            parse_projects("staging=abcdefgh,\nproduction=ijklmnop"),
            [("staging", "abcdefgh"), ("production", "ijklmnop")],
        )
        with self.assertRaises(SupabaseAdvisorError):
            parse_projects("staging=abcdefgh,staging=ijklmnop")

    def test_remote_fetch_uses_only_get_advisor_endpoints(self) -> None:
        responses = [
            FakeResponse({"lints": [lint("security_lint", "WARN", "secret_table")]}),
            FakeResponse({"lints": [lint("performance_lint", "INFO", "private_index")]}),
        ]
        with patch("docgov.supabase_remote.urllib.request.urlopen", side_effect=responses) as request:
            result = fetch_advisors("abcdefghijklmnopqrst", "secret-token")
        self.assertEqual(set(result), {"security", "performance"})
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            req = call.args[0]
            self.assertEqual(req.method, "GET")
            self.assertIn("/advisors/", req.full_url)
            self.assertEqual(req.get_header("Authorization"), "Bearer secret-token")

    def test_snapshot_is_change_sensitive_but_redacts_entity_details(self) -> None:
        first = build_evidence_snapshot(
            "staging",
            "abcdefghijklmnopqrst",
            {
                "security": [lint("auth_lint", "WARN", "secret_table")],
                "performance": [],
            },
            observed_at="2026-09-05T00:00:00Z",
        )
        later = build_evidence_snapshot(
            "staging",
            "abcdefghijklmnopqrst",
            {
                "security": [lint("auth_lint", "WARN", "secret_table", "2026-09-06T00:00:00Z")],
                "performance": [],
            },
            observed_at="2026-09-06T00:00:00Z",
        )
        rendered = json.dumps(first)
        self.assertNotIn("secret_table", rendered)
        self.assertNotIn("private detail", rendered)
        self.assertNotIn("abcdefghijklmnopqrst", rendered)
        self.assertEqual(first["fingerprint_sha256"], later["fingerprint_sha256"])
        self.assertEqual(first["advisors"]["security"]["level_counts"], {"WARN": 1})

    def test_immutable_evidence_is_written_only_when_advisor_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = build_evidence_snapshot(
                "production",
                "abcdefghijklmnopqrst",
                {"security": [lint("auth_lint", "WARN", "one")], "performance": []},
                observed_at="2026-09-05T00:00:00Z",
            )
            same = dict(first, observed_at="2026-09-06T00:00:00Z")
            changed = build_evidence_snapshot(
                "production",
                "abcdefghijklmnopqrst",
                {"security": [lint("auth_lint", "WARN", "two")], "performance": []},
                observed_at="2026-09-06T00:00:00Z",
            )
            first_write = write_evidence(root, first)
            same_write = write_evidence(root, same)
            changed_write = write_evidence(root, changed)
            self.assertTrue(first_write.changed)
            self.assertFalse(same_write.changed)
            self.assertTrue(changed_write.changed)
            self.assertEqual(len(list(root.rglob("*.json"))), 2)

    def test_collection_fetches_all_projects_before_writing_any_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "docgov.supabase_remote.fetch_advisors",
                side_effect=[
                    {"security": [], "performance": []},
                    SupabaseAdvisorError("second project failed"),
                ],
            ):
                with self.assertRaises(SupabaseAdvisorError):
                    collect_advisor_evidence(
                        root,
                        [("staging", "abcdefgh"), ("production", "ijklmnop")],
                        "token",
                    )
            self.assertFalse((root / "docs/security-evidence").exists())


if __name__ == "__main__":
    unittest.main()


def _snapshot(environment: str, lints: list, observed_at: str) -> dict:
    return build_evidence_snapshot(
        environment,
        "abcdefgh12345678",
        {"security": lints, "performance": []},
        observed_at=observed_at,
    )


class EnvironmentDriftTests(unittest.TestCase):
    """Read-only detection of a production that no commit produced (WP4, D7)."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.ledger_path = self.root / ".docgov/ledger.jsonl"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def fingerprints(self, **environments) -> dict:
        return {
            name: environment_fingerprint(snapshot)
            for name, snapshot in environments.items()
        }

    def record_promotion(self, fingerprint: str) -> Ledger:
        ledger = Ledger(self.ledger_path)
        ledger.append(
            run_id="release-1",
            document="supabase:production",
            action=PROMOTION_ACTION,
            reason="Promoted the release branch to production.",
            dependency_fingerprint=fingerprint,
        )
        return ledger

    def test_matching_fingerprints_produce_no_finding(self) -> None:
        shared = [lint("auth_rls_disabled", "ERROR", "public.profiles")]
        staging = _snapshot("staging", shared, "2026-09-05T00:00:00Z")
        production = _snapshot("production", shared, "2026-09-05T00:05:00Z")
        ledger = self.record_promotion(environment_fingerprint(production).advisor_fingerprint)

        findings = compare_environments(
            self.fingerprints(staging=staging, production=production), ledger
        )
        self.assertEqual(findings, [])

    def test_production_changed_outside_the_release_flow_is_high_risk_drift(self) -> None:
        shared = [lint("auth_rls_disabled", "ERROR", "public.profiles")]
        promoted = _snapshot("production", shared, "2026-09-05T00:00:00Z")
        ledger = self.record_promotion(environment_fingerprint(promoted).advisor_fingerprint)

        # Somebody changed production from the dashboard: same categories, new state.
        changed = _snapshot(
            "production",
            shared + [lint("auth_rls_disabled", "ERROR", "public.orders")],
            "2026-09-06T00:00:00Z",
        )
        staging = _snapshot("staging", shared, "2026-09-06T00:00:00Z")

        findings = compare_environments(
            self.fingerprints(staging=staging, production=changed), ledger
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "environment_drift")
        self.assertEqual(findings[0].risk, "high")
        self.assertEqual(findings[0].action, "block")

    def test_a_production_only_category_is_drift_even_without_a_promotion_record(self) -> None:
        staging = _snapshot("staging", [lint("auth_rls_disabled", "ERROR", "public.profiles")], "2026-09-05T00:00:00Z")
        production_lint = lint("unindexed_foreign_keys", "WARN", "public.orders")
        production_lint["categories"] = ["PERFORMANCE"]
        production = _snapshot("production", [production_lint], "2026-09-05T00:00:00Z")

        findings = compare_environments(
            self.fingerprints(staging=staging, production=production), Ledger(self.ledger_path)
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("no longer predicts", findings[0].reason)

    def test_no_promotion_record_means_rule_one_cannot_fire(self) -> None:
        shared = [lint("auth_rls_disabled", "ERROR", "public.profiles")]
        staging = _snapshot("staging", shared, "2026-09-05T00:00:00Z")
        production = _snapshot("production", shared + [lint("auth_rls_disabled", "ERROR", "public.orders")], "2026-09-05T00:00:00Z")

        findings = compare_environments(
            self.fingerprints(staging=staging, production=production), Ledger(self.ledger_path)
        )
        self.assertEqual(findings, [])

    def test_an_unevaluable_environment_is_drift_not_agreement(self) -> None:
        """No promotion record and no reference evidence means nothing was checked."""
        production = _snapshot("production", [lint("auth_rls_disabled", "ERROR", "public.profiles")], "2026-09-05T00:00:00Z")
        findings = compare_environments(
            self.fingerprints(production=production), Ledger(self.ledger_path)
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].risk, "high")
        self.assertIn("cannot be checked", findings[0].reason)

    def test_a_missing_production_snapshot_is_not_treated_as_agreement(self) -> None:
        staging = _snapshot("staging", [lint("auth_rls_disabled", "ERROR", "public.profiles")], "2026-09-05T00:00:00Z")
        self.assertEqual(
            compare_environments(self.fingerprints(staging=staging), Ledger(self.ledger_path)),
            [],
        )

    def test_findings_never_expose_a_project_ref(self) -> None:
        shared = [lint("auth_rls_disabled", "ERROR", "public.profiles")]
        promoted = _snapshot("production", shared, "2026-09-05T00:00:00Z")
        ledger = self.record_promotion(environment_fingerprint(promoted).advisor_fingerprint)
        production_lint = lint("unindexed_foreign_keys", "WARN", "public.orders")
        production_lint["categories"] = ["PERFORMANCE"]
        production = _snapshot("production", shared + [production_lint], "2026-09-06T00:00:00Z")
        staging = _snapshot("staging", shared, "2026-09-06T00:00:00Z")

        findings = compare_environments(
            self.fingerprints(staging=staging, production=production), ledger
        )
        serialized = json.dumps([finding.to_dict() for finding in findings])
        self.assertNotIn("abcdefgh12345678", serialized)
        self.assertNotIn("public.orders", serialized)


class DriftCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        self.catalog_path = self.root / ".docgov/catalog.yaml"
        self.ledger_path = self.root / ".docgov/ledger.jsonl"
        self.trust_state_path = self.root / DEFAULT_TRUST_STATE_PATH
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path.write_text(json.dumps({
            "version": 1,
            "taxonomy": {"state": ["docs/status/**"], "contract": ["docs/architecture/**"]},
            "documents": [{
                "path": "docs/status/PRODUCTION.md",
                "type": "state",
                "status": "current",
                "last_verified_at": _iso_now(),
                "depends_on": ["ops/production/**"],
                "environments": ["production"],
            }],
            "policies": {},
        }), encoding="utf-8")
        (self.root / "docs/status").mkdir(parents=True, exist_ok=True)
        (self.root / "docs/status/PRODUCTION.md").write_text("# Production\n", encoding="utf-8")
        (self.root / "ops/production").mkdir(parents=True, exist_ok=True)
        (self.root / "ops/production/inventory.json").write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)
        record_baseline(
            build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path),
            self.ledger_path,
            approved=True,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_snapshot(self, environment: str, lints: list, observed_at: str) -> None:
        write_evidence(self.root, _snapshot(environment, lints, observed_at))

    def test_drift_blocks_the_documents_that_describe_the_drifted_environment(self) -> None:
        shared = [lint("auth_rls_disabled", "ERROR", "public.profiles")]
        self.write_snapshot("staging", shared, "2026-09-05T00:00:00Z")
        production_lint = lint("unindexed_foreign_keys", "WARN", "public.orders")
        production_lint["categories"] = ["PERFORMANCE"]
        self.write_snapshot("production", shared + [production_lint], "2026-09-05T00:01:00Z")

        exit_code = cli_main([
            "--root", str(self.root),
            "drift", "--environments", "staging,production", "--apply",
        ])
        self.assertEqual(exit_code, 2)

        state = load_trust_state(self.trust_state_path)
        entry = trust_entries(state)["docs/status/PRODUCTION.md"]
        self.assertFalse(entry.usable)
        self.assertEqual(state["drifted_environments"], ["production"])

        supply = DocumentSupply(SupplyConfig(
            root=self.root,
            trust_state_path=self.trust_state_path,
            catalog_path=self.catalog_path,
        ))
        self.assertEqual(supply.get_document("docs/status/PRODUCTION.md")["status"], "refused")

    def test_aligned_environments_clear_the_drift_and_restore_the_documents(self) -> None:
        shared = [lint("auth_rls_disabled", "ERROR", "public.profiles")]
        self.write_snapshot("staging", shared, "2026-09-05T00:00:00Z")
        self.write_snapshot("production", shared, "2026-09-05T00:01:00Z")

        exit_code = cli_main([
            "--root", str(self.root),
            "drift", "--environments", "staging,production", "--apply",
        ])
        self.assertEqual(exit_code, 0)
        entry = trust_entries(load_trust_state(self.trust_state_path))["docs/status/PRODUCTION.md"]
        self.assertTrue(entry.usable)

    def test_drift_never_deploys_and_needs_no_write_credential(self) -> None:
        """The command reads committed evidence; without --collect it opens no socket."""
        self.write_snapshot("staging", [lint("auth_rls_disabled", "ERROR", "public.profiles")], "2026-09-05T00:00:00Z")
        with patch("docgov.supabase_remote.urllib.request.urlopen", side_effect=AssertionError("network")):
            self.assertEqual(
                cli_main(["--root", str(self.root), "drift", "--environments", "staging,production"]),
                0,
            )

    def test_losing_the_reference_evidence_cannot_clear_a_recorded_drift(self) -> None:
        """The append-only ledger makes a wrongly cleared drift permanent, so it must not happen."""
        shared = [lint("auth_rls_disabled", "ERROR", "public.profiles")]
        self.write_snapshot("staging", shared, "2026-09-05T00:00:00Z")
        production_lint = lint("unindexed_foreign_keys", "WARN", "public.orders")
        production_lint["categories"] = ["PERFORMANCE"]
        self.write_snapshot("production", shared + [production_lint], "2026-09-05T00:01:00Z")
        cli_main(["--root", str(self.root), "drift", "--environments", "staging,production", "--apply"])
        self.assertFalse(
            trust_entries(load_trust_state(self.trust_state_path))["docs/status/PRODUCTION.md"].usable
        )

        # A staging fetch failed, so only production is in scope this run.
        exit_code = cli_main(["--root", str(self.root), "drift", "--environments", "production", "--apply"])
        self.assertEqual(exit_code, 2)
        self.assertFalse(
            trust_entries(load_trust_state(self.trust_state_path))["docs/status/PRODUCTION.md"].usable
        )

    def test_record_promotion_stores_the_baseline_rule_one_compares_against(self) -> None:
        shared = [lint("auth_rls_disabled", "ERROR", "public.profiles")]
        self.write_snapshot("production", shared, "2026-09-05T00:00:00Z")
        cli_main([
            "--root", str(self.root),
            "drift", "--environments", "production", "--apply", "--record-promotion", "production",
        ])
        promotions = [
            entry for entry in Ledger(self.ledger_path).entries()
            if entry.get("action") == PROMOTION_ACTION
        ]
        self.assertEqual(len(promotions), 1)

        # A later production state that no promotion explains is drift.
        self.write_snapshot(
            "production", shared + [lint("auth_rls_disabled", "ERROR", "public.orders")], "2026-09-06T00:00:00Z"
        )
        self.assertEqual(
            cli_main(["--root", str(self.root), "drift", "--environments", "production", "--apply"]),
            2,
        )
