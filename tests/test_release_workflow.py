"""Structural checks for the release workflow's safety gates."""

import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - requirements-dev.txt installs PyYAML
    yaml = None  # type: ignore[assignment]

WORKFLOWS = Path(__file__).parents[1] / ".github/workflows"
GATES = {"ci": "ci.yml", "examples": "examples.yml", "lint": "workflow-lint.yml"}


def load(name):
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # PyYAML reads the bare key "on" as the boolean True.
    document["on"] = document.pop(True, document.get("on"))
    return document


@unittest.skipUnless(yaml, "needs PyYAML")
class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.release = load("release.yml")
        self.jobs = self.release["jobs"]

    def test_release_runs_only_from_main_in_this_repository(self):
        condition = self.jobs["release"]["if"]
        self.assertEqual(
            condition,
            "github.repository == 'alex-papaioannou/python-ci-workflows' && github.ref == 'refs/heads/main'",
        )

    def test_release_waits_for_every_gate_without_overrides(self):
        release = self.jobs["release"]
        self.assertEqual(set(release["needs"]), set(GATES))
        # A status function or an "or" in the condition would let a failed gate through.
        self.assertNotRegex(release["if"], r"always\(|failure\(|cancelled\(|\|\|")

    def test_gates_run_the_pull_request_workflows_on_main(self):
        for job, workflow in GATES.items():
            with self.subTest(job=job):
                self.assertEqual(
                    self.jobs[job]["uses"], f"./.github/workflows/{workflow}"
                )
                self.assertEqual(
                    self.jobs[job]["if"], "github.ref == 'refs/heads/main'"
                )
                self.assertIn("workflow_call", load(workflow)["on"])

    def test_only_the_release_job_can_write(self):
        self.assertEqual(self.release["permissions"], {"contents": "read"})
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                expected = {"contents": "write"} if name == "release" else None
                self.assertEqual(job.get("permissions"), expected)
        self.assertEqual(self.jobs["release"]["environment"], "release")

    def test_release_checkout_can_see_the_parent_commit(self):
        checkout = self.jobs["release"]["steps"][1]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["fetch-depth"], 2)
        self.assertIs(checkout["with"]["persist-credentials"], False)

    def test_gated_calls_never_share_concurrency_groups_or_artifact_names(self):
        # Calls in one release run share github.run_id, so each python-ci call must
        # differ in working-directory or call-name (see python-ci.yml concurrency).
        calls = []
        for workflow in ("ci.yml", "examples.yml"):
            for job in load(workflow)["jobs"].values():
                if job.get("uses") == "./.github/workflows/python-ci.yml":
                    inputs = job.get("with", {})
                    calls.append(
                        (
                            inputs.get("working-directory", "."),
                            inputs.get("call-name", ""),
                        )
                    )
        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual(len(calls), len(set(calls)), calls)


class ReleaseTriggerTests(unittest.TestCase):
    def test_release_triggers_on_changelog_pushes_to_main(self):
        text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        self.assertRegex(
            text, r"(?m)^  push:\n    branches: \[main\]\n    paths: \[CHANGELOG\.md\]$"
        )
