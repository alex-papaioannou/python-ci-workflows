"""Structural checks for the release workflow's safety gates."""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).parents[1] / ".github/workflows"
RELEASE = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")


def job(name):
    match = re.search(
        rf"^  {name}:\n(.*?)(?=^  [a-z-]+:\n|\Z)", RELEASE, re.MULTILINE | re.DOTALL
    )
    assert match, f"release.yml has no job {name!r}"
    return match.group(1)


class ReleaseGateTests(unittest.TestCase):
    def test_every_job_is_limited_to_main_in_this_repository(self):
        for name in ("ci", "examples", "lint", "release"):
            self.assertIn(
                "if: github.repository == 'alex-papaioannou/python-ci-workflows' && github.ref == 'refs/heads/main'",
                job(name),
                name,
            )

    def test_release_waits_for_every_check(self):
        self.assertIn("needs: [ci, examples, lint]", job("release"))

    def test_checks_call_the_same_workflows_as_pull_requests(self):
        for name, workflow in (
            ("ci", "ci.yml"),
            ("examples", "examples.yml"),
            ("lint", "workflow-lint.yml"),
        ):
            self.assertIn(f"uses: ./.github/workflows/{workflow}", job(name))
            self.assertRegex(
                (WORKFLOWS / workflow).read_text(encoding="utf-8"),
                r"(?m)^  workflow_call:$",
                workflow,
            )

    def test_only_the_release_job_can_write(self):
        self.assertIn("environment: release", job("release"))
        self.assertIn("contents: write", job("release"))
        self.assertEqual(RELEASE.count("contents: write"), 1)
        self.assertRegex(RELEASE, r"(?m)^permissions:\n  contents: read$")

    def test_gated_calls_never_share_concurrency_groups_or_artifact_names(self):
        # Calls in one release run share github.run_id, so each python-ci call must
        # differ in working-directory or call-name (see python-ci.yml concurrency).
        calls = []
        for workflow in ("ci.yml", "examples.yml"):
            text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
            for block in re.split(r"(?m)^  (?=[a-z-]+:\n)", text)[1:]:
                if "uses: ./.github/workflows/python-ci.yml" not in block:
                    continue
                directory = re.search(r"working-directory: (\S+)", block)
                call_name = re.search(r"call-name: (\S+)", block)
                calls.append(
                    (
                        directory.group(1) if directory else ".",
                        call_name.group(1) if call_name else "",
                    )
                )
        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual(len(calls), len(set(calls)), calls)
