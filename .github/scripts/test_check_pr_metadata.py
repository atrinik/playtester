"""Regression tests for pull-request metadata release policy."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "check_pr_metadata.py"
SPEC = importlib.util.spec_from_file_location("check_pr_metadata", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class PullRequestMetadataTests(unittest.TestCase):
    def test_accepts_full_url_for_nonclosing_cross_repository_link(self) -> None:
        body = "Part of https://github.com/atrinik/atrinik/issues/357"
        self.assertEqual([], POLICY.validation_errors("fix: keep links honest", body))

    def test_rejects_ambiguous_nonclosing_cross_repository_shorthand(self) -> None:
        for prefix in ("Part of", "Refs", "Related:"):
            with self.subTest(prefix=prefix):
                errors = POLICY.validation_errors(
                    "fix: keep links honest",
                    "{} atrinik/atrinik#357".format(prefix),
                )
                self.assertEqual(1, len(errors))
                self.assertIn("ambiguous nonclosing reference", errors[0])

    def test_allows_explicit_closing_shorthand_and_local_references(self) -> None:
        body = (
            "Closes atrinik/playtester#24, atrinik/atrinik#357\n"
            "Refs #24"
        )
        self.assertEqual([], POLICY.validation_errors("fix: keep links honest", body))

    def test_rejects_nonconventional_title(self) -> None:
        errors = POLICY.validation_errors("Keep links honest", "")
        self.assertEqual(1, len(errors))
        self.assertIn("Conventional Commits", errors[0])

    def test_rejects_cross_repository_shorthand_in_squash_subject(self) -> None:
        errors = POLICY.validation_errors("fix: follow atrinik/atrinik#357", "")
        self.assertEqual(1, len(errors))
        self.assertIn("PR title uses ambiguous", errors[0])

    def test_workflows_use_trusted_policy_and_run_regressions(self) -> None:
        policy_workflow = (ROOT / ".github" / "workflows" / "pr-title.yml").read_text(
            encoding="utf-8"
        )
        validation_workflow = (
            ROOT / ".github" / "workflows" / "validate.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("github.event.repository.default_branch", policy_workflow)
        self.assertIn("PR_BODY", policy_workflow)
        self.assertIn(".github/scripts/check_pr_metadata.py", policy_workflow)
        self.assertIn("test_check_pr_metadata.py", validation_workflow)


if __name__ == "__main__":
    unittest.main()
