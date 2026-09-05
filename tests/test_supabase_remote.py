from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docgov.supabase_remote import (
    SupabaseAdvisorError,
    build_evidence_snapshot,
    collect_advisor_evidence,
    fetch_advisors,
    parse_projects,
    write_evidence,
)


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
