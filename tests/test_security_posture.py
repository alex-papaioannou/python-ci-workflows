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
PRE_TEST_HOOK = "./.github/actions/python-ci-pre-test"

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


def key_paths(node, name, path=()):
    """Yield the path to every mapping key called name, ignoring case, in parsed YAML."""
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() == name:
                yield (*path, key)
            yield from key_paths(value, name, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from key_paths(value, name, (*path, index))


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
        # "./" resolves in the caller's checkout, so the only such reference is the
        # caller's own pre-test hook; "$/" is this repository at the commit of the
        # workflow. A job's own uses: calls a reusable workflow.
        references = [(where, step.get("uses")) for where, step in steps()]
        references += [
            (f"{WORKFLOW.name} / {name}", job.get("uses"))
            for name, job in self.workflow["jobs"].items()
        ]
        local = [
            (where, uses) for where, uses in references if (uses or "")[:2] == "./"
        ]
        self.assertEqual(
            local, [(f"{WORKFLOW.name} / test / Run the pre-test hook", PRE_TEST_HOOK)]
        )
        for where, uses in references:
            if uses and uses[:2] not in ("$/", "./"):
                with self.subTest(uses=where):
                    self.assertRegex(uses, r"@[0-9a-f]{40}$")

    def test_a_call_that_uploads_dist_restores_no_pip_cache(self):
        (setup,) = (
            step
            for step in self.workflow["jobs"]["test"]["steps"]
            if step.get("uses", "").startswith("actions/setup-python@")
        )
        # The caller publishes the dist artifact, so no cell of that call may restore
        # a pip cache that another run, on any trigger, could have written.
        self.assertEqual(
            setup["with"]["cache"],
            "${{ !inputs.upload-dist && (inputs.requirements-file != ''"
            " || inputs.install-project) && 'pip' || '' }}",
        )

    def test_the_workflow_never_declares_a_cache_mode(self):
        # A caller's cache-mode caps the workflows it calls, and a called workflow
        # that asks for more than its caller allows does not start. Comments may
        # mention the key; only a declaration counts.
        self.assertEqual(list(key_paths(self.workflow, "cache-mode")), [])
        # The check finds a declaration at each level where one could appear.
        text = WORKFLOW.read_text(encoding="utf-8")
        job_steps = self.workflow["jobs"]["test"]["steps"]
        validate = [step.get("name") for step in job_steps].index("Validate inputs")
        for anchor, line, path in (
            (
                "\npermissions:\n  contents: read\n",
                "cache-mode: read\n",
                ("cache-mode",),
            ),
            ("\n  test:\n", "    cache-mode: read\n", ("jobs", "test", "cache-mode")),
            (
                "      - name: Validate inputs\n",
                "        Cache-Mode: none\n",
                ("jobs", "test", "steps", validate, "Cache-Mode"),
            ),
        ):
            with self.subTest(path=path):
                self.assertIn(anchor, text)
                planted = yaml.safe_load(text.replace(anchor, anchor + line, 1))
                self.assertEqual(list(key_paths(planted, "cache-mode")), [path])


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
            "`upload-dist`",
            "`cache-mode`",
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
