"""Keep the security posture that the README promises callers of the workflow."""

import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - requirements-dev.txt installs PyYAML
    yaml = None  # type: ignore[assignment]

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/python-ci.yml"
ACTION = ROOT / ".github/actions/python-ci/action.yml"
STEP_SCRIPTS = "$/.github/actions/python-ci"

# Expression names ignore case. Besides secrets.X, a declared or inherited secrets:
# block, this matches github.token, github['token'] and the whole github context
# (github.*, toJSON(github)), which all carry the token secrets.GITHUB_TOKEN holds.
SECRET_REFERENCE = (
    r"(?i)\bsecrets\b"
    r"|\bgithub\s*(?:\.\s*(?:token\b|\*)|\[)"
    r"|\btojson\s*\(\s*github\s*\)"
)


def load(path):
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML reads the bare key "on" as the boolean True.
    if True in document:
        document["on"] = document.pop(True)
    return document


def steps():
    """Yield (where, step) for every step of the workflow and of its action."""
    for job_name, job in load(WORKFLOW)["jobs"].items():
        for step in job.get("steps", []):
            name = step.get("name", step.get("uses"))
            yield f"{WORKFLOW.name} / {job_name} / {name}", step
    for step in load(ACTION)["runs"]["steps"]:
        yield f"{ACTION.name} / {step.get('name')}", step


@unittest.skipUnless(yaml, "needs PyYAML")
class SecurityPostureTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load(WORKFLOW)

    def test_jobs_get_only_contents_read(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        for name, job in self.workflow["jobs"].items():
            with self.subTest(job=name):
                # A job's own permissions replace the top-level ones, so they may
                # only narrow them. "read-all" and "write-all" are not mappings.
                permissions = job.get("permissions", {})
                self.assertIsInstance(permissions, dict)
                widened = {
                    scope: level
                    for scope, level in permissions.items()
                    if level != "none" and (scope, level) != ("contents", "read")
                }
                self.assertEqual(widened, {})

    def test_no_secrets_are_referenced(self):
        for path in (WORKFLOW, ACTION):
            with self.subTest(file=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"), SECRET_REFERENCE)

    def test_checkout_does_not_persist_credentials(self):
        checkouts = [
            (where, step)
            for where, step in steps()
            if step.get("uses", "").startswith("actions/checkout@")
        ]
        self.assertTrue(checkouts)
        for where, step in checkouts:
            with self.subTest(step=where):
                self.assertIs(step.get("with", {}).get("persist-credentials"), False)

    def test_run_scripts_contain_no_expressions(self):
        # The runner pastes an expression's value into the script before the shell
        # parses it, so inputs reach the scripts through env: blocks instead.
        for where, step in steps():
            if "run" in step:
                with self.subTest(step=where):
                    self.assertNotIn("${{", step["run"])

    def test_no_project_code_runs_before_inputs_are_validated(self):
        # Only checkout, setup-python and the step-script action may come first; none
        # of them runs code from the caller's repository.
        first = ("actions/checkout@", "actions/setup-python@", STEP_SCRIPTS)
        job_steps = self.workflow["jobs"]["test"]["steps"]
        validate = [step.get("name") for step in job_steps].index("Validate inputs")
        for step in job_steps[:validate]:
            with self.subTest(step=step.get("name", step.get("uses"))):
                self.assertTrue(step.get("uses", "").startswith(first))
        # The step-script action only runs its own script, never another action.
        for step in load(ACTION)["runs"]["steps"]:
            with self.subTest(step=step.get("name")):
                self.assertNotIn("uses", step)

    def test_requirements_are_hash_checked_by_default(self):
        inputs = self.workflow["on"]["workflow_call"]["inputs"]
        self.assertIs(inputs["require-hashes"]["default"], True)

    def test_actions_are_pinned_to_commit_shas(self):
        # "./" would resolve in the caller's checkout; "$/" is this repository at the
        # commit of the workflow. A job's own uses: calls a reusable workflow.
        references = [(where, step.get("uses")) for where, step in steps()]
        references += [
            (f"{WORKFLOW.name} / {name}", job.get("uses"))
            for name, job in self.workflow["jobs"].items()
        ]
        for where, uses in references:
            if uses and not uses.startswith("$/"):
                with self.subTest(uses=where):
                    self.assertRegex(uses, r"@[0-9a-f]{40}$")


class PostureDocumentationTests(unittest.TestCase):
    def test_readme_and_contributing_state_each_guarantee(self):
        guarantees = (
            "full commit SHA",
            "`permissions: contents: read`",
            "`secrets`",
            "`github.token`",
            "`persist-credentials: false`",
            "`${{ }}`",
            "`Validate inputs`",
            "`require-hashes`",
            "breaking change",
        )
        for name, title, link in (
            ("README.md", "Security posture", "](SECURITY.md)"),
            ("CONTRIBUTING.md", "Design rules", "](README.md#security-posture)"),
        ):
            text = (ROOT / name).read_text(encoding="utf-8")
            section = re.search(rf"(?ms)^## {title}\n(.*?)^## ", text)
            self.assertIsNotNone(section, f"{name} has no '## {title}' section")
            body = " ".join(section.group(1).split())
            for phrase in (*guarantees, link):
                with self.subTest(file=name, phrase=phrase):
                    self.assertIn(phrase, body)
