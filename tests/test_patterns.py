from __future__ import annotations

import unittest

from docgov.patterns import matches_repo_glob


class RepositoryGlobTests(unittest.TestCase):
    def test_single_star_does_not_cross_directory_boundary(self) -> None:
        self.assertTrue(matches_repo_glob("docs/status.json", "docs/*.json"))
        self.assertFalse(matches_repo_glob("docs/evidence/status.json", "docs/*.json"))

    def test_double_star_matches_nested_paths(self) -> None:
        self.assertTrue(matches_repo_glob("supabase/functions/a/index.ts", "supabase/functions/**"))
        self.assertTrue(matches_repo_glob("supabase/functions/a/lib/x.ts", "supabase/functions/**"))
        self.assertTrue(matches_repo_glob("README.md", "**/README.md"))
        self.assertTrue(matches_repo_glob("supabase/README.md", "**/README.md"))

    def test_question_mark_and_character_classes_remain_segment_scoped(self) -> None:
        self.assertTrue(matches_repo_glob("docs/v1.md", "docs/v[0-9].md"))
        self.assertFalse(matches_repo_glob("docs/nested/v1.md", "docs/v?.md"))


if __name__ == "__main__":
    unittest.main()
