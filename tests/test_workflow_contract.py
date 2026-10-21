"""Run the workflow's step scripts against temporary consumer projects."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import unittest.mock
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - requirements-dev.txt installs PyYAML
    yaml = None  # type: ignore[assignment]

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/python-ci.yml"
STEPS = ROOT / ".github/actions/python-ci/steps.py"
SMOKE = ROOT / ".github/workflows/ci.yml"
EXAMPLES = ROOT / ".github/workflows/examples.yml"
DEV_REQUIREMENTS = ROOT / "requirements-dev.txt"
# The caller's pre-test hook, relative to the root of the caller's checkout.
PRE_TEST_HOOK = ".github/actions/python-ci-pre-test/action.yml"

# Variables the workflow sets from its inputs and step results. Inner runs must not
# inherit them from the outer environment (this repository's own CI sets several for
# its test step).
INPUT_VARIABLES = set(
    re.findall(
        r"^\s+([A-Z_]+): \$\{\{ (?:inputs|steps)\.", WORKFLOW.read_text(), re.MULTILINE
    )
) | {
    "PYTHONWARNINGS",
    "PYTHONHASHSEED",
}


def clean_environment(**overrides):
    """Return the outer environment without workflow inputs, plus overrides."""
    base = {
        name: value for name, value in os.environ.items() if name not in INPUT_VARIABLES
    }
    return {**base, **overrides}


def script(name):
    """Return the steps.py subcommand that the named workflow step runs."""
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("      - name: " + name)
    for line in lines[start + 1 :]:
        if line.startswith("      - "):
            break
        match = re.fullmatch(r'        run: python "\$PYTHON_CI_STEPS" ([a-z-]+)', line)
        if match:
            return match.group(1)
    raise AssertionError(f"Step {name!r} does not run a step script")


def input_readers(name):
    """List the job settings and steps outside 'Validate inputs' that read an input."""
    # inputs.name or inputs['name'], as json.dumps escapes quotes. Expressions
    # ignore case in property names, so inputs.Name reads the same input.
    quoted = re.escape(name)
    reads = re.compile(
        rf"inputs(\.{quoted}(?![\w-])|\[\s*\\?['\"]{quoted}\\?['\"]\s*\])",
        re.IGNORECASE,
    )
    readers = []
    for job_name, job in yaml.safe_load(WORKFLOW.read_text())["jobs"].items():
        for key, value in job.items():
            if key != "steps" and reads.search(json.dumps(value)):
                readers.append(f"{job_name}: {key}")
        for step in job.get("steps", []):
            step_name = step.get("name") or step.get("uses")
            if step_name != "Validate inputs" and reads.search(json.dumps(step)):
                readers.append(f"{job_name}: {step_name}")
    return readers


def markdown_section(name, title):
    """Return the body of a level-two section of a repository Markdown file."""
    text = (ROOT / name).read_text(encoding="utf-8")
    section = re.search(rf"(?ms)^## {re.escape(title)}\n(.*?)^## ", text)
    if section is None:
        raise AssertionError(f"{name} has no '## {title}' section")
    return section.group(1)


# Callers download artifacts by name, so these are public interface. Each upload
# step's name is the documented name, with the matrix cell for <os>-<python>, plus
# the call suffix.
ARTIFACT_NAMES = ("dist", "junit-<os>-<python>", "coverage-<os>-<python>")
MATRIX_CELL = (
    "${{ steps.validate.outputs.os-slug || matrix.os }}-${{ matrix.python-version }}"
)
CALL_SUFFIX = "${{ steps.validate.outputs.artifact-suffix }}"


def artifact_template(name):
    """Return the upload step's artifact name template for a documented name."""
    return name.replace("<os>-<python>", MATRIX_CELL) + CALL_SUFFIX


class StepHarness:
    def run_step(self, root, step, env=None, executable=sys.executable, cwd=None):
        """Run a step script with its own RUNNER_TEMP and system temp directory.

        Steps must leave nothing in the system temp directory, which persistent
        self-hosted runners never clean; scratch files belong under RUNNER_TEMP.
        """
        runner_temp = tempfile.mkdtemp(prefix="runner-temp-")
        system_temp = tempfile.mkdtemp(prefix="system-temp-")
        for path in (runner_temp, system_temp):
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        process_env = clean_environment(
            GITHUB_WORKSPACE=str(root),
            GITHUB_STEP_SUMMARY=os.devnull,
            GITHUB_OUTPUT=os.devnull,
            GITHUB_ENV=os.devnull,
            RUNNER_TEMP=runner_temp,
            TMPDIR=system_temp,
            TEMP=system_temp,
            TMP=system_temp,
        )
        process_env.update(env or {})
        result = subprocess.run(
            [executable, str(STEPS), script(step)],
            check=False,
            cwd=cwd or root,
            env=process_env,
            text=True,
            capture_output=True,
        )
        # pytest keeps its tmp_path base directories there and prunes old ones itself.
        leaked = sorted(
            p.name
            for p in Path(system_temp).iterdir()
            if not p.name.startswith("pytest-of-")
        )
        self.assertEqual(leaked, [], f"{step} left files in the system temp directory")
        return result

    def execute(
        self,
        files,
        env=None,
        step="Run tests and reject empty test suites",
        executable=sys.executable,
        working_directory=".",
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    path.write_bytes(content)
                else:
                    path.write_text(content)
            return self.run_step(
                root, step, env, executable, cwd=root / working_directory
            )


class WorkflowTests(StepHarness, unittest.TestCase):
    def test_success_and_consumer_root_import(self):
        result = self.execute(
            {
                "lib.py": "VALUE=7",
                "tests/test_ok.py": "import unittest\nfrom lib import VALUE\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(VALUE,7)",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failure_propagates(self):
        result = self.execute(
            {
                "tests/test_bad.py": 'import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail("expected")'
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected", result.stderr)

    def test_empty_suite_rejected(self):
        result = self.execute({"tests/empty.py": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No tests discovered", result.stderr)

    def test_import_error_rejected(self):
        result = self.execute(
            {"tests/test_import.py": "import nonexistent_fixture_module"}
        )
        self.assertNotEqual(result.returncode, 0)

    def test_syntax_error_rejected(self):
        result = self.execute({"bad.py": "def broken("}, step="Check Python syntax")
        self.assertNotEqual(result.returncode, 0)

    def test_custom_test_directory(self):
        result = self.execute(
            {
                "checks/test_ok.py": "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass"
            },
            {"TEST_DIRECTORY": "checks"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_custom_pattern_excludes_other_tests(self):
        result = self.execute(
            {
                "tests/check_ok.py": "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass",
                "tests/test_bad.py": "raise RuntimeError()",
            },
            {"TEST_PATTERN": "check_*.py"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unittest_treats_empty_inputs_as_its_conventions(self):
        files = {
            "tests/test_ok.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
        }
        result = self.execute(files, {"TEST_DIRECTORY": "", "TEST_PATTERN": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def test_explicit_top_level_directory(self):
        result = self.execute(
            {
                "pkg/__init__.py": "",
                "pkg/tests/__init__.py": "",
                "pkg/tests/test_ok.py": "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass",
            },
            {"TEST_DIRECTORY": "pkg/tests", "TOP_LEVEL_DIRECTORY": "."},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_quiet_verbosity(self):
        result = self.execute(
            {
                "tests/test_ok.py": "import unittest\nclass T(unittest.TestCase):\n def test_unique_method(self): pass"
            },
            {"TEST_VERBOSITY": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("test_unique_method", result.stderr)

    def test_fail_fast_stops_after_first_failure(self):
        result = self.execute(
            {
                "tests/test_bad.py": "import unittest\nclass T(unittest.TestCase):\n def test_a(self): self.fail()\n def test_b(self): self.fail()"
            },
            {"TEST_FAIL_FAST": "true"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Ran 1 test", result.stderr)

    def test_compilation_targets_exclude_unselected_files(self):
        result = self.execute(
            {"good.py": "x=1", "bad.py": "def broken("},
            {"COMPILE_PATHS": '["good.py"]'},
            step="Check Python syntax",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compilation_exclusion(self):
        result = self.execute(
            {"good.py": "x=1", "generated/bad.py": "def broken("},
            {"COMPILE_EXCLUDE": "generated"},
            step="Check Python syntax",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_input_rejected_before_execution(self):
        for env in (
            {"COMPILE_PATHS": "[]"},
            {"COMPILE_PATHS": '"."'},
            {"COMPILE_PATHS": '["../"]'},
            {"TEST_VERBOSITY": "9"},
            {"COMPILE_EXCLUDE": "["},
        ):
            result = self.execute({"tests/test_ok.py": ""}, env, step="Validate inputs")
            self.assertNotEqual(result.returncode, 0, env)

    def test_warnings_can_fail_tests(self):
        result = self.execute(
            {
                "tests/test_warn.py": 'import unittest, warnings\nclass T(unittest.TestCase):\n def test_warn(self): warnings.warn("deprecated")'
            },
            {"PYTHONWARNINGS": "error"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deprecated", result.stderr)

    def test_actions_pinned_to_commit_shas(self):
        for workflow in WORKFLOW.parent.glob("*.yml"):
            for line in workflow.read_text().splitlines():
                if "uses:" in line and not re.search(r"uses: (\./|\$/)", line):
                    self.assertRegex(
                        line, r"@[0-9a-f]{40}\b", f"{workflow.name}: {line}"
                    )

    def test_missing_requirements_file_rejected(self):
        result = self.execute(
            {"tests/test_ok.py": ""},
            {"REQUIREMENTS_FILE": "missing.txt"},
            step="Validate inputs",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requirements-file", result.stderr)

    def test_unhashed_requirements_rejected(self):
        result = self.execute(
            {"requirements.txt": "./local\n", "local/pyproject.toml": ""},
            {"REQUIREMENTS_FILE": "requirements.txt", "PIP_NO_INDEX": "1"},
            step="Install dependencies",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hash", result.stderr.lower())

    def test_empty_requirements_install(self):
        result = self.execute(
            {"requirements.txt": "# none\n"},
            {"REQUIREMENTS_FILE": "requirements.txt", "PIP_NO_INDEX": "1"},
            step="Install dependencies",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_lint_inputs_rejected(self):
        for env in (
            {"LINT_PATHS": "[]"},
            {"LINT_PATHS": '["missing"]'},
            {"RUFF_VERSION": "latest"},
        ):
            result = self.execute({"tests/test_ok.py": ""}, env, step="Validate inputs")
            self.assertNotEqual(result.returncode, 0, env)

    def test_ruff_reports_lint_failures(self):
        env = {"RUFF_VERSION": "0.17.0", "RUFF_FORMAT": "false", "LINT_PATHS": '["."]'}
        bad = self.execute({"bad.py": "import os\n"}, env, step="Lint with Ruff")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("F401", bad.stdout)
        good = self.execute({"good.py": "VALUE = 1\n"}, env, step="Lint with Ruff")
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)

    def test_mypy_reports_type_errors(self):
        env = {"MYPY_VERSION": "2.4.0", "LINT_PATHS": '["."]'}
        bad = self.execute(
            {"bad.py": "VALUE: int = 'text'\n"}, env, step="Type-check with mypy"
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("incompatible", bad.stdout.lower())
        good = self.execute(
            {"good.py": "VALUE: int = 1\n"}, env, step="Type-check with mypy"
        )
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        invalid = self.execute(
            {"tests/test_ok.py": ""}, {"MYPY_VERSION": "newest"}, step="Validate inputs"
        )
        self.assertNotEqual(invalid.returncode, 0)

    @unittest.skipUnless(
        importlib.util.find_spec("coverage"), "coverage is not installed"
    )
    def test_coverage_threshold(self):
        files = {
            "lib.py": "def used():\n    return 1\n\n\ndef unused():\n    return 2\n",
            "tests/test_lib.py": "import unittest\nfrom lib import used\n\n\nclass T(unittest.TestCase):\n    def test_used(self):\n        self.assertEqual(used(), 1)\n",
        }
        env = {"COVERAGE_VERSION": "7.16.2", "COVERAGE_FAIL_UNDER": "100"}
        low = self.execute(files, env)
        self.assertNotEqual(low.returncode, 0)
        self.assertIn("below the required 100%", low.stderr)
        env["COVERAGE_FAIL_UNDER"] = "50"
        with tempfile.TemporaryDirectory() as out:
            summary = Path(out) / "summary.md"
            ok = self.execute(files, {**env, "GITHUB_STEP_SUMMARY": str(summary)})
            self.assertEqual(ok.returncode, 0, ok.stderr)
            text = summary.read_text(encoding="utf-8")
            self.assertIn("#### Coverage: 88.9%", text)
            self.assertRegex(text, r"\| lib.py +\| +4 \| +1 \| +75% \| +6 \|")
        for value in ("101", "-1", "most"):
            invalid = self.execute(
                {"tests/test_ok.py": ""},
                {"COVERAGE_FAIL_UNDER": value},
                step="Validate inputs",
            )
            self.assertNotEqual(invalid.returncode, 0, value)

    def test_package_build_requires_pyproject(self):
        result = self.execute(
            {"tests/test_ok.py": ""}, {"BUILD_PACKAGE": "true"}, step="Validate inputs"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pyproject.toml", result.stderr)

    def test_package_build_and_metadata_check(self):
        pyproject = (
            '[build-system]\nrequires = ["flit_core>=3.9,<4"]\nbuild-backend = "flit_core.buildapi"\n\n'
            '[project]\nname = "fixture-pkg"\nversion = "1.0"\ndescription = "Fixture"\n'
            'readme = "README.md"\n'
        )
        files = {
            "pyproject.toml": pyproject,
            "README.md": "Fixture\n",
            "fixture_pkg/__init__.py": "",
        }
        result = self.execute(files, step="Build and check package")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASSED", result.stdout)

    def test_default_max_parallel_covers_the_default_matrix(self):
        text = WORKFLOW.read_text()

        def default(name):
            block = re.search(
                rf"^      {name}:\n(?:        .*\n)*?        default: (.*)$",
                text,
                re.MULTILINE,
            )
            return block.group(1)

        versions = json.loads(json.loads(default("python-versions")))
        systems = json.loads(json.loads(default("operating-systems")))
        self.assertGreaterEqual(
            int(default("max-parallel")), len(versions) * len(systems)
        )
        self.assertEqual(int(default("max-parallel")), 256)

    def test_concurrency_is_opt_in(self):
        text = WORKFLOW.read_text()
        self.assertIn("cancel-in-progress: ${{ inputs.cancel-in-progress }}", text)
        self.assertIn("github.run_id, github.run_attempt", text)

    def test_concurrency_groups_are_scoped_to_each_call(self):
        group = re.search(
            r"^      group: (.*)$", WORKFLOW.read_text(), re.MULTILINE
        ).group(1)
        branches = group.split(" || ")
        self.assertEqual(len(branches), 2)
        for branch in branches:
            self.assertIn(
                "inputs.working-directory, inputs.call-name, strategy.job-index", branch
            )

    def test_artifact_suffix_keeps_names_unique_per_call(self):
        cases = (
            ({}, ""),
            ({"WORKING_DIRECTORY": "."}, ""),
            ({"WORKING_DIRECTORY": "./"}, ""),
            ({"WORKING_DIRECTORY": "examples/src-package"}, "-examples-src-package"),
            ({"WORKING_DIRECTORY": "./services/api/"}, "-services-api"),
            # Spaces are kept, unlike in the runner part of the name.
            ({"WORKING_DIRECTORY": "services\\web app"}, "-services-web app"),
            (
                {"WORKING_DIRECTORY": "services/api", "CALL_NAME": "api-lint"},
                "-api-lint",
            ),
        )
        for env, suffix in cases:
            with self.subTest(env=env):
                values, _ = self.validate_outputs(env)
                self.assertEqual(values["artifact-suffix"], suffix)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_artifact_names_are_public_interface(self):
        # Publish jobs and report tools download the artifacts by name, so changing
        # a name is a breaking change. A new artifact joins ARTIFACT_NAMES and the
        # list in CONTRIBUTING.md.
        names = [
            step.get("with", {}).get("name")
            for job in yaml.safe_load(WORKFLOW.read_text())["jobs"].values()
            for step in job.get("steps", [])
            if step.get("uses", "").startswith("actions/upload-artifact")
        ]
        self.assertCountEqual(names, [artifact_template(n) for n in ARTIFACT_NAMES])

    def validate_outputs(self, env):
        with tempfile.TemporaryDirectory() as out:
            output = Path(out) / "output"
            result = self.execute(
                {"tests/test_ok.py": ""},
                {**env, "GITHUB_OUTPUT": str(output)},
                step="Validate inputs",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
            return values, result.stdout

    def test_primary_cell_skips_experimental_versions(self):
        matrix = {
            "PYTHON_VERSIONS": '["3.15", "3.14"]',
            "EXPERIMENTAL_PYTHON_VERSIONS": '["3.15"]',
            "OPERATING_SYSTEMS": '["ubuntu-latest", "windows-latest"]',
        }
        expected = {
            ("ubuntu-latest", "3.15"): "false",
            ("ubuntu-latest", "3.14"): "true",
            ("windows-latest", "3.14"): "false",
        }
        for (system, version), primary in expected.items():
            with self.subTest(system=system, version=version):
                values, _ = self.validate_outputs(
                    {**matrix, "MATRIX_OS": f'"{system}"', "MATRIX_PYTHON": version}
                )
                self.assertEqual(values["primary"], primary)
                self.assertEqual(values["os-slug"], system)

    def test_primary_cell_falls_back_when_every_version_is_experimental(self):
        matrix = {
            "PYTHON_VERSIONS": '["3.15", "3.14"]',
            "EXPERIMENTAL_PYTHON_VERSIONS": '["3.14", "3.15"]',
            "OPERATING_SYSTEMS": '["ubuntu-latest"]',
            "MATRIX_OS": '"ubuntu-latest"',
        }
        for version, primary in (("3.15", "true"), ("3.14", "false")):
            with self.subTest(version=version):
                values, _ = self.validate_outputs({**matrix, "MATRIX_PYTHON": version})
                self.assertEqual(values["primary"], primary)

    def test_runner_label_arrays_and_groups(self):
        cases = (
            (
                '[["self-hosted", "linux"]]',
                '[\n  "self-hosted",\n  "linux"\n]',
                "self-hosted-linux",
            ),
            (
                '[{"group": "big runners", "labels": ["linux"]}]',
                '{"group": "big runners", "labels": ["linux"]}',
                "big-runners-linux",
            ),
        )
        for systems, matrix_os, slug in cases:
            with self.subTest(systems=systems):
                values, _ = self.validate_outputs(
                    {
                        "OPERATING_SYSTEMS": systems,
                        "PYTHON_VERSIONS": '["3.13"]',
                        "MATRIX_OS": matrix_os,
                        "MATRIX_PYTHON": "3.13",
                    }
                )
                self.assertEqual((values["primary"], values["os-slug"]), ("true", slug))

    def test_matrix_configuration_warnings(self):
        cases = (
            (
                {
                    "PYTHON_VERSIONS": '["3.13"]',
                    "EXPERIMENTAL_PYTHON_VERSIONS": '["3.31"]',
                },
                "lists 3.31, which is not in python-versions",
            ),
            (
                {
                    "PYTHON_VERSIONS": '["3.15"]',
                    "EXPERIMENTAL_PYTHON_VERSIONS": '["3.15"]',
                },
                "no failure can fail the run",
            ),
            ({"PYTHON_VERSIONS": "[3.12, 3.13]"}, "quote them"),
        )
        for env, warning in cases:
            with self.subTest(env=env):
                _, stdout = self.validate_outputs(env)
                self.assertIn("::warning::", stdout)
                self.assertIn(warning, stdout)

    def test_once_per_run_checks_use_the_primary_cell(self):
        text = WORKFLOW.read_text()
        for step in ("Audit dependencies", "Lint with Ruff", "Build and check package"):
            block = text[text.index(f"- name: {step}") :].split("\n      - ", 1)[0]
            with self.subTest(step=step):
                self.assertIn("steps.validate.outputs.primary == 'true'", block)
                self.assertNotIn("fromJSON(inputs.python-versions)[0]", block)

    def test_matrix_exclude_validated(self):
        matrix = {
            "PYTHON_VERSIONS": '["3.12", "3.13", "3.14t"]',
            "EXPERIMENTAL_PYTHON_VERSIONS": '["3.14t"]',
            "OPERATING_SYSTEMS": '["ubuntu-latest", "windows-latest"]',
        }
        rejected = (
            ('["windows-latest"]', "must be an object"),
            ('[{"os": "windows-latest", "arch": "arm64"}]', "must be an object"),
            ('[{"os": "macos-latest"}]', "not in operating-systems"),
            ('[{"python-version": "3.9"}]', "not in python-versions"),
            (
                '[{"os": "ubuntu-latest", "python-version": "3.12"}]',
                "removes the primary cell",
            ),
            (
                '[{"python-version": "3.12"}, {"python-version": "3.13"}, {"python-version": "3.14t"}]',
                "removes every matrix cell",
            ),
        )
        for value, message in rejected:
            with self.subTest(value=value):
                result = self.execute(
                    {"tests/test_ok.py": ""},
                    {**matrix, "MATRIX_EXCLUDE": value},
                    step="Validate inputs",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        accepted = '[{"os": "windows-latest", "python-version": "3.14t"}, {"os": "windows-latest", "python-version": "3.12"}]'
        for value in ("[]", accepted):
            with self.subTest(value=value):
                result = self.execute(
                    {"tests/test_ok.py": ""},
                    {**matrix, "MATRIX_EXCLUDE": value},
                    step="Validate inputs",
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_matrix_exclude_matches_like_github(self):
        # GitHub matches strings without case, arrays by prefix and objects by subset.
        arrays = '[["self-hosted", "linux", "gpu"], ["self-hosted", "linux"]]'
        groups = '[{"group": "ci", "labels": ["linux", "x64"]}, {"group": "ci", "labels": ["linux"]}]'
        cases = (
            (
                arrays,
                '[{"os": ["self-hosted", "linux"], "python-version": "3.12"}]',
                False,
            ),
            (
                arrays,
                '[{"os": ["self-hosted", "linux", "gpu"], "python-version": "3.13"}]',
                True,
            ),
            (
                groups,
                '[{"os": {"group": "ci", "labels": ["linux"]}, "python-version": "3.12"}]',
                False,
            ),
            (groups, '[{"os": {"group": "CI"}, "python-version": "3.13"}]', True),
            ('["ubuntu-latest", "windows-latest"]', '[{"os": "Windows-Latest"}]', True),
            (
                '["ubuntu-latest", "windows-latest"]',
                '[{"os": "Ubuntu-Latest", "python-version": "3.12"}]',
                False,
            ),
            (
                '["ubuntu-latest", "windows-latest"]',
                '[{"os": "windows-latest", "python-version": 3.13}]',
                True,
            ),
        )
        for systems, exclude, ok in cases:
            with self.subTest(systems=systems, exclude=exclude):
                result = self.execute(
                    {"tests/test_ok.py": ""},
                    {
                        "OPERATING_SYSTEMS": systems,
                        "PYTHON_VERSIONS": '["3.12", "3.13"]',
                        "MATRIX_EXCLUDE": exclude,
                    },
                    step="Validate inputs",
                )
                self.assertEqual(result.returncode == 0, ok, result.stderr)
                if not ok:
                    self.assertIn("removes the primary cell", result.stderr)

    def test_matrix_exclude_is_wired_into_the_matrix(self):
        self.assertIn(
            "exclude: ${{ fromJSON(inputs.matrix-exclude) }}", WORKFLOW.read_text()
        )

    def test_call_name_validated(self):
        for value, ok in (
            ("api", True),
            ("svc-1.web_2", True),
            ("", True),
            ("a/b", False),
            ("a b", False),
        ):
            with self.subTest(value=value):
                result = self.execute(
                    {"tests/test_ok.py": ""},
                    {"CALL_NAME": value},
                    step="Validate inputs",
                )
                self.assertEqual(result.returncode == 0, ok, result.stderr)
                if not ok:
                    self.assertIn("call-name", result.stderr)

    COVERED_LIB = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    CLI = "import lib\n\nprint(lib.b())\n"

    @unittest.skipUnless(
        importlib.util.find_spec("coverage"), "coverage is not installed"
    )
    def test_coverage_includes_subprocesses_with_unittest(self):
        tests = (
            "import subprocess, sys, unittest\n\nfrom lib import a\n\n\nclass T(unittest.TestCase):\n"
            "    def test_a(self):\n        self.assertEqual(a(), 1)\n\n"
            "    def test_cli(self):\n        out = subprocess.run([sys.executable, 'cli.py'], capture_output=True, text=True, check=True)\n"
            "        self.assertEqual(out.stdout.strip(), '2')\n"
        )
        files = {
            "lib.py": self.COVERED_LIB,
            "cli.py": self.CLI,
            "tests/test_lib.py": tests,
        }
        result = self.execute_keep(
            files, {"COVERAGE_VERSION": "7.16.2", "COVERAGE_FAIL_UNDER": "100"}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"cli\.py +2 +0 +100%")
        self.assertEqual(self.leftover, [])

    @unittest.skipUnless(
        importlib.util.find_spec("coverage"), "coverage is not installed"
    )
    def test_coverage_ignores_stale_fragments_and_leaves_none_on_failure(self):
        tests = "import unittest\n\nfrom lib import a\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(a(), 1)\n"
        files = {
            "lib.py": self.COVERED_LIB,
            "tests/test_lib.py": tests,
            ".coverage.stale.1.x": "not coverage data",
        }
        result = self.execute_keep(
            files, {"COVERAGE_VERSION": "7.16.2", "COVERAGE_FAIL_UNDER": "100"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("below the required 100%", result.stderr)
        self.assertRegex(result.stdout, r"lib\.py +4 +1 +75%")
        self.assertEqual(self.leftover, [".coverage.stale.1.x"])

    @unittest.skipUnless(
        importlib.util.find_spec("coverage"), "coverage is not installed"
    )
    def test_subprocesses_stay_quiet_with_a_project_include(self):
        tests = (
            "import subprocess, sys, unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_cli(self):\n        out = subprocess.run([sys.executable, 'cli.py'], capture_output=True, text=True, check=True)\n"
            "        self.assertEqual(out.stderr, '')\n"
        )
        files = {
            "lib.py": self.COVERED_LIB,
            "cli.py": self.CLI,
            "pyproject.toml": '[tool.coverage.run]\ninclude = ["lib.py", "cli.py"]\n',
            "tests/test_cli.py": tests,
        }
        result = self.execute_keep(files, {"COVERAGE_VERSION": "7.16.2"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(
        importlib.util.find_spec("coverage"), "coverage is not installed"
    )
    def test_coverage_warns_when_nothing_was_measured(self):
        test = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
        cases = {
            "everything omitted": {
                "pyproject.toml": '[tool.coverage.run]\nomit = ["*"]\n',
                "tests/test_ok.py": test,
            },
            # The project is recorded as unexecuted source, so files were "measured".
            "project never imported": {
                "pyproject.toml": '[tool.coverage.run]\nomit = ["tests/*"]\n',
                "lib.py": self.COVERED_LIB,
                "tests/test_ok.py": test,
            },
        }
        for case, files in cases.items():
            with self.subTest(case=case):
                result = self.execute_keep(files, {"COVERAGE_VERSION": "7.16.2"})
                self.assertIn("::warning::Coverage collected no data", result.stdout)
        uses_lib = "import unittest\n\nfrom lib import a\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(a(), 1)\n"
        files = {"lib.py": self.COVERED_LIB, "tests/test_lib.py": uses_lib}
        result = self.execute_keep(files, {"COVERAGE_VERSION": "7.16.2"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Coverage collected no data", result.stdout)

    @unittest.skipUnless(
        importlib.util.find_spec("coverage"), "coverage is not installed"
    )
    def test_coverage_honours_project_omit_settings(self):
        tests = "import unittest\n\nfrom lib import a\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(a(), 1)\n"
        files = {
            "lib.py": self.COVERED_LIB,
            "unused.py": "def never():\n    return 3\n",
            "pyproject.toml": '[tool.coverage.run]\nomit = ["unused.py"]\nbranch = true\n',
            "tests/test_lib.py": tests,
        }
        result = self.execute_keep(files, {"COVERAGE_VERSION": "7.16.2"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("unused.py", result.stdout)
        self.assertIn("Branch", result.stdout)

    @unittest.skipUnless(
        importlib.util.find_spec("coverage"), "coverage is not installed"
    )
    def test_coverage_with_a_pyproject_that_has_no_coverage_section(self):
        tests = "import unittest\n\nfrom lib import a\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(a(), 1)\n"
        files = {
            "lib.py": self.COVERED_LIB,
            "pyproject.toml": '[project]\nname = "x"\nversion = "1"\n\n[tool.pytest.ini_options]\nxfail_strict = true\n',
            "tests/test_lib.py": tests,
        }
        result = self.execute_keep(files, {"COVERAGE_VERSION": "7.16.2"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"lib\.py +4 +1 +75%")

    @unittest.skipUnless(
        importlib.util.find_spec("coverage") and importlib.util.find_spec("xdist"),
        "needs coverage and pytest-xdist",
    )
    def test_coverage_measures_pytest_xdist_workers(self):
        tests = (
            "import subprocess, sys\n\nfrom lib import a\n\n\ndef test_a():\n    assert a() == 1\n\n\n"
            "def test_cli():\n    out = subprocess.run([sys.executable, 'cli.py'], capture_output=True, text=True, check=True)\n"
            "    assert out.stdout.strip() == '2'\n"
        )
        files = {
            "lib.py": self.COVERED_LIB,
            "cli.py": self.CLI,
            "tests/test_lib.py": tests,
        }
        env = {
            **PYTEST,
            "PYTEST_ARGS": "-n 2",
            "COVERAGE_VERSION": "7.16.2",
            "COVERAGE_FAIL_UNDER": "100",
        }
        for warnings in ("default", "error"):
            with self.subTest(warnings=warnings):
                result = self.execute_keep(files, {**env, "PYTHONWARNINGS": warnings})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertRegex(result.stdout, r"TOTAL +\d+ +0 +100%")
                self.assertEqual(self.leftover, [])

    def execute_keep(self, files, env):
        """Run the test step and record coverage fragment files left in the project."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            result = self.run_step(root, "Run tests and reject empty test suites", env)
            self.leftover = sorted(p.name for p in root.glob(".coverage.*"))
            return result

    def test_junit_report_and_step_summary(self):
        import xml.etree.ElementTree as ET

        tests = (
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n\n"
            "    def test_bad(self):\n        self.fail('boom')\n\n"
            "    @unittest.skip('later')\n    def test_skip(self):\n        pass\n"
        )
        with tempfile.TemporaryDirectory() as out:
            summary = Path(out) / "summary.md"
            report = Path(out) / "reports/junit.xml"
            result = self.execute(
                {"tests/test_mix.py": tests},
                {"JUNIT_FILE": str(report), "GITHUB_STEP_SUMMARY": str(summary)},
            )
            self.assertNotEqual(result.returncode, 0)
            root = ET.parse(report).getroot()
            self.assertEqual(
                (
                    root.get("tests"),
                    root.get("failures"),
                    root.get("errors"),
                    root.get("skipped"),
                ),
                ("3", "1", "0", "1"),
            )
            self.assertIn("boom", root.find("testcase/failure").text)
            text = summary.read_text()
            self.assertIn("| 1 | 1 | 0 | 1 |", text)
            self.assertIn("test_mix.T.test_bad", text)

    def run_with_reports(self, files):
        import xml.etree.ElementTree as ET

        with tempfile.TemporaryDirectory() as out:
            summary = Path(out) / "summary.md"
            report = Path(out) / "junit.xml"
            result = self.execute(
                files, {"JUNIT_FILE": str(report), "GITHUB_STEP_SUMMARY": str(summary)}
            )
            return (
                result,
                ET.parse(report).getroot(),
                summary.read_text(encoding="utf-8"),
            )

    def test_failing_subtests_reach_the_summary_and_junit(self):
        tests = (
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n\n"
            "    def test_values(self):\n        for v in (1, 2, 3):\n"
            "            with self.subTest(v=v):\n                self.assertNotEqual(v, 2)\n\n"
            "    def test_errors(self):\n        for v in (1, 2):\n"
            "            with self.subTest(v=v):\n                if v == 2:\n                    raise KeyError(v)\n"
        )
        result, root, summary = self.run_with_reports({"tests/test_sub.py": tests})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED (failures=1, errors=1)", result.stderr)
        self.assertEqual((root.get("failures"), root.get("errors")), ("1", "1"))
        names = {case.get("name") for case in root.iter("testcase")}
        self.assertIn("test_values (v=2)", names)
        self.assertIn("test_errors (v=2)", names)
        self.assertIn("| 1 | 1 | 1 | 0 |", summary)
        self.assertIn("test_sub.T.test_values (v=2)", summary)

    def test_class_fixture_errors_are_named_after_the_class(self):
        tests = (
            "import time, unittest\n\n\nclass A(unittest.TestCase):\n"
            "    def test_slow(self):\n        time.sleep(0.3)\n\n\n"
            "class B(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
            "        raise RuntimeError('fixture')\n\n    def test_never(self):\n        pass\n"
        )
        result, root, _ = self.run_with_reports({"tests/test_fix.py": tests})
        self.assertNotEqual(result.returncode, 0)
        error = next(c for c in root.iter("testcase") if c.find("error") is not None)
        self.assertEqual(
            (error.get("classname"), error.get("name")), ("test_fix.B", "setUpClass")
        )
        self.assertLess(float(error.get("time")), 0.1)
        self.assertIn("fixture", error.find("error").text)

    def test_control_characters_still_give_valid_junit(self):
        tests = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_color(self):\n        self.fail('\\x1b[31mred\\x1b[0m \\x00 mismatch')\n"
        result, root, _ = self.run_with_reports({"tests/test_ctl.py": tests})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mismatch", root.find("testcase/failure").text)

    def test_pipes_in_test_ids_stay_in_one_table_cell(self):
        tests = (
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_pipe(self):\n"
            "        with self.subTest(case='a|b'):\n            self.fail('x')\n"
        )
        _, _, summary = self.run_with_reports({"tests/test_pipe.py": tests})
        row = next(
            line
            for line in summary.splitlines()
            if line.startswith("| `") and "test_pipe" in line
        )
        self.assertIn("a\\|b", row)
        self.assertEqual(row.replace("\\|", "").count("|"), 3)

    def test_junit_path_must_stay_in_checkout(self):
        result = self.execute(
            {"tests/test_ok.py": ""},
            {"JUNIT_FILE": "../junit.xml"},
            step="Validate inputs",
        )
        self.assertNotEqual(result.returncode, 0)

    def test_prerelease_versions_are_opt_in_and_non_blocking(self):
        text = WORKFLOW.read_text()
        self.assertIn("allow-prereleases: ${{ inputs.allow-prereleases }}", text)
        self.assertIn(
            "contains(fromJSON(inputs.experimental-python-versions), matrix.python-version)",
            text,
        )

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_result_job_reports_matrix_outcome_without_python(self):
        job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["result"]
        self.assertEqual(job["needs"], "test")
        self.assertEqual(job["if"], "always()")
        # A label, or JSON for a label array or runner group, as one operating-systems entry.
        self.assertEqual(
            job["runs-on"],
            "${{ (startsWith(inputs.result-runner, '[') || startsWith(inputs.result-runner, '{'))"
            " && fromJSON(inputs.result-runner) || inputs.result-runner }}",
        )
        self.assertEqual(job["outputs"]["result"], "${{ needs.test.result }}")
        # One step that fails unless the matrix succeeded, and nothing that needs
        # Python or this repository's action.
        (step,) = job["steps"]
        self.assertEqual(step["if"], "needs.test.result != 'success'")
        self.assertEqual(step["run"], "exit 1")
        self.assertNotIn("uses", step)
        self.assertNotIn("shell", step)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_result_check_keeps_its_job_id_and_name(self):
        # Callers require "<caller job> / result" in branch protection, and a
        # required check that no job reports any more stays pending.
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        self.assertIn("result", jobs)
        self.assertEqual(jobs["result"].get("name"), "result")
        # A matrix would add its values to the check name, as in "result (linux)".
        self.assertNotIn("strategy", jobs["result"])

    def declared(self, block):
        """Return the inputs or outputs the workflow declares, each described."""
        lines = WORKFLOW.read_text().splitlines()
        start = lines.index(f"    {block}:", lines.index("  workflow_call:")) + 1
        declared = []
        for index, line in enumerate(lines[start:], start):
            if not line.startswith("      "):
                break
            if re.fullmatch(r"      [a-z-]+:", line):
                declared.append(line.strip()[:-1])
                self.assertTrue(
                    lines[index + 1].strip().startswith("description: "), line
                )
        return set(declared)

    def documented(self, title):
        """Return the names in the first column of a README section's tables."""
        section = markdown_section("README.md", title)
        return set(re.findall(r"^\| `([a-z-]+)` \|", section, re.MULTILINE))

    def test_every_input_is_described_and_documented(self):
        self.assertEqual(self.declared("inputs"), self.documented("Inputs"))

    def test_every_output_is_described_and_documented(self):
        outputs = self.declared("outputs")
        self.assertTrue(outputs)
        self.assertEqual(outputs, self.documented("Outputs and required checks"))

    def test_public_interface_is_documented(self):
        # CONTRIBUTING.md lists what callers may depend on, and its Releases section
        # makes changing any of it a breaking change. The README tells callers.
        listed = [f"`{name}`" for name in (*ARTIFACT_NAMES, *self.declared("outputs"))]
        listed += ["`<caller job> / result`", "call suffix"]
        not_listed = (
            "step names",
            "log text",
            "job summary layout",
            "matrix test jobs",
        )
        bodies = {
            name: " ".join(markdown_section(name, title).split()).lower()
            for name, title in (
                ("CONTRIBUTING.md", "Public interface"),
                ("README.md", "Outputs and required checks"),
            )
        }
        for name, body in bodies.items():
            for phrase in (*listed, *not_listed):
                with self.subTest(file=name, phrase=phrase):
                    self.assertIn(phrase, body)
        self.assertIn("primary-cell rule", bodies["CONTRIBUTING.md"])
        self.assertIn("`.github/workflows/python-ci.yml`", bodies["CONTRIBUTING.md"])
        releases = markdown_section("CONTRIBUTING.md", "Releases")
        self.assertIn("](#public-interface)", releases)
        policy = markdown_section("README.md", "Support policy")
        self.assertIn("](CONTRIBUTING.md#public-interface)", policy)

    def test_support_policy_matches_the_workflow_and_ci(self):
        def section(title):
            return " ".join(markdown_section("README.md", title).split())

        policy = section("Support policy")
        # The support window is the default python-versions, a run of minor versions.
        default = re.search(
            r"^      python-versions:\n(?:        .*\n)*?        default: (.*)$",
            WORKFLOW.read_text(),
            re.MULTILINE,
        ).group(1)
        versions = json.loads(json.loads(default))
        minors = [int(version.split(".")[1]) for version in versions]
        self.assertEqual(minors, list(range(minors[0], minors[-1] + 1)))
        self.assertIn(f"{versions[0]} through {versions[-1]} in v", policy)
        # Every runner label this repository's CI tests on is named.
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        labels = re.search(r"^      operating-systems: '(.*)'$", ci, re.MULTILINE)
        for label in json.loads(labels.group(1)):
            self.assertIn(f"`{label}`", policy)
        # The self-hosted runner floor is the one "How it runs" gives.
        floor = r"runner (?:on )?version (\d+\.\d+\.\d+) or newer"
        (runner_floor,) = re.findall(floor, section("How it runs"))
        self.assertEqual(re.findall(floor, policy), [runner_floor])

    def test_more_invalid_inputs_rejected(self):
        cases = (
            {"TEST_PATTERN": "tests/test_*.py"},
            {"TEST_FAIL_FAST": "maybe"},
            {"TEST_DIRECTORY": "missing"},
            {"TOP_LEVEL_DIRECTORY": "missing"},
            {"COMPILE_PATHS": "not json"},
            {"COMPILE_PATHS": '[""]'},
            {"LINT_PATHS": '["../"]'},
            {"REQUIREMENTS_FILE": "tests"},
        )
        for env in cases:
            result = self.execute({"tests/test_ok.py": ""}, env, step="Validate inputs")
            self.assertNotEqual(result.returncode, 0, env)

    def test_valid_inputs_accepted(self):
        env = {
            "COMPILE_PATHS": '["src", "tests"]',
            "LINT_PATHS": '["src"]',
            "REQUIREMENTS_FILE": "requirements.txt",
            "RUFF_VERSION": "0.17.0",
            "MYPY_VERSION": "2.4",
            "COVERAGE_VERSION": "7",
            "COVERAGE_FAIL_UNDER": "87.5",
            "JUNIT_FILE": "reports/junit.xml",
            "TEST_VERBOSITY": "1",
            "TEST_FAIL_FAST": "true",
        }
        files = {"tests/test_ok.py": "", "src/app.py": "", "requirements.txt": ""}
        result = self.execute(files, env, step="Validate inputs")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unhashed_requirements_allowed_when_hashes_not_required(self):
        result = self.execute(
            {"requirements.txt": "./local\n", "local/pyproject.toml": ""},
            {
                "REQUIREMENTS_FILE": "requirements.txt",
                "REQUIRE_HASHES": "false",
                "PIP_NO_INDEX": "1",
            },
            step="Install dependencies",
        )
        self.assertNotIn("hash", result.stderr.lower())

    def test_errors_in_tests_fail_the_run(self):
        result = self.execute(
            {
                "tests/test_err.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_err(self):\n        raise KeyError('x')\n"
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("KeyError", result.stderr)


def github_env_entries(text):
    """Return the (name, value) pairs a GITHUB_ENV file sets, read as the runner does.

    An entry is NAME=VALUE on one line, or a NAME<<DELIMITER line followed by the
    value's lines and a line that is exactly the delimiter.
    """
    entries = []
    lines = iter(text.split("\n"))
    for line in lines:
        if not line:
            continue
        equals, heredoc = line.find("="), line.find("<<")
        if heredoc < 0 or 0 <= equals < heredoc:
            name, value = line.split("=", 1)
            entries.append((name, value))
            continue
        name, delimiter = line.split("<<", 1)
        value_lines = []
        for value_line in lines:
            if value_line == delimiter:
                break
            value_lines.append(value_line)
        else:
            raise AssertionError(f"{name} has no closing delimiter {delimiter!r}")
        entries.append((name, "\n".join(value_lines)))
    return entries


class CallerEnvTests(StepHarness, unittest.TestCase):
    def validate(self, value, **env):
        """Run 'Validate inputs' with the env input; return the result and GITHUB_ENV."""
        with tempfile.TemporaryDirectory() as directory:
            github_env = Path(directory, "github_env")
            result = self.execute(
                {"tests/test_ok.py": ""},
                {**env, "CALLER_ENV": value, "GITHUB_ENV": str(github_env)},
                step="Validate inputs",
            )
            text = ""
            if github_env.exists():
                with open(github_env, encoding="utf-8", newline="") as file:
                    text = file.read()
        return result, text

    def assert_rejected(self, value, message):
        result, text = self.validate(value)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        # Every message starts with the input's name.
        self.assertRegex(result.stderr, rf"(?m)^env {re.escape(message)}")
        self.assertEqual(text, "")

    def test_variables_are_set_for_later_steps_in_heredoc_form(self):
        variables = {"DJANGO_SETTINGS_MODULE": "mysite.settings", "FEATURE_FLAG": "on"}
        result, text = self.validate(json.dumps(variables))
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in variables:
            self.assertRegex(text, rf"(?m)^{name}<<\S+$")
        self.assertEqual(github_env_entries(text), list(variables.items()))
        self.assertIn("env sets DJANGO_SETTINGS_MODULE, FEATURE_FLAG", result.stdout)

    def test_the_default_sets_nothing(self):
        for value in ("{}", ""):
            with self.subTest(value=value):
                result, text = self.validate(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(text, "")

    def test_a_value_with_newlines_stays_one_variable(self):
        # Lines shaped like a delimiter, an assignment or a heredoc header are value.
        cases = {
            "delimiter-shaped lines": (
                "first\nEOF\nPYTHON_CI_ENV_" + "0" * 32 + "\nOTHER=1\nOTHER<<EOF\nlast"
            ),
            "Windows line endings": "first\r\nsecond\r\n",
            "a trailing newline": "only\n",
            "an empty value": "",
        }
        for case, value in cases.items():
            with self.subTest(case=case):
                result, text = self.validate(json.dumps({"MULTI": value}))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(github_env_entries(text), [("MULTI", value)])

    def test_a_delimiter_the_value_contains_is_drawn_again(self):
        steps = load_steps()
        taken = "PYTHON_CI_ENV_" + "a" * 32
        value = f"before\n{taken}\nafter"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "github_env")
            with (
                unittest.mock.patch.dict(os.environ, {"GITHUB_ENV": str(path)}),
                unittest.mock.patch.object(
                    steps.secrets, "token_hex", side_effect=["a" * 32, "b" * 32]
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                steps.export_env({"MULTI": value})
            with open(path, encoding="utf-8", newline="") as file:
                text = file.read()
        self.assertTrue(text.startswith(f"MULTI<<PYTHON_CI_ENV_{'b' * 32}\n"), text)
        self.assertEqual(github_env_entries(text), [("MULTI", value)])

    def test_reserved_names_are_rejected(self):
        # Windows ignores case in variable names, so every spelling is rejected.
        names = (
            "GITHUB_TOKEN",
            "GITHUB_PATH",
            "github_output",
            "RUNNER_TEMP",
            "ACTIONS_RUNTIME_TOKEN",
            "PATH",
            "path",
            "Path",
            "NODE_OPTIONS",
            "BASH_ENV",
            "ENV",
            "env",
            "SHELLOPTS",
            "shellopts",
            "BASHOPTS",
            "PS4",
            "PYTHONHASHSEED",
            "PYTHONWARNINGS",
            "pythonwarnings",
            "PYTHON_CI_STEPS",
            "TEST_RUNNER",
            "CALLER_ENV",
        )
        for name in names:
            with self.subTest(name=name):
                self.assert_rejected(json.dumps({name: "x"}), f"may not set {name}:")

    def test_every_variable_the_workflow_maps_is_rejected(self):
        steps = load_steps()
        for name in sorted(INPUT_VARIABLES):
            for spelling in (name, name.lower()):
                with self.subTest(name=spelling):
                    with (
                        unittest.mock.patch.dict(
                            os.environ, {"CALLER_ENV": json.dumps({spelling: "x"})}
                        ),
                        self.assertRaises(SystemExit) as raised,
                    ):
                        steps.caller_env()
                    self.assertIn(f"env may not set {spelling}:", str(raised.exception))

    def test_invalid_names_are_rejected(self):
        for name in ("1ST", "MY-VAR", "MY VAR", "", "A=B", "NAME\nOTHER", "\u00c4PFEL"):
            with self.subTest(name=name):
                self.assert_rejected(
                    json.dumps({name: "x"}),
                    f"has an invalid variable name {json.dumps(name)}",
                )

    def test_values_that_are_not_strings_are_rejected(self):
        for value in (1, 1.5, True, None, ["on"], {"on": "yes"}):
            with self.subTest(value=value):
                self.assert_rejected(
                    json.dumps({"FLAG": value}),
                    f"must give FLAG a string value, not {json.dumps(value)}",
                )
        self.assert_rejected(
            json.dumps({"FLAG": "a\0b"}), "gives FLAG a value with a NUL character"
        )
        # A lone surrogate, which UTF-8 cannot encode, after a value that can be.
        self.assert_rejected(
            json.dumps({"FIRST": "on", "FLAG": "\ud800"}),
            "gives FLAG a value that is not valid Unicode",
        )

    def test_the_harness_leaves_the_outer_github_env_alone(self):
        # This repository's CI runs the suite inside a job with its own GITHUB_ENV.
        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory, "github_env")
            outer.write_text("")
            with unittest.mock.patch.dict(os.environ, {"GITHUB_ENV": str(outer)}):
                result = self.execute(
                    {"tests/test_ok.py": ""},
                    {"CALLER_ENV": json.dumps({"FLAG": "on"})},
                    step="Validate inputs",
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(outer.read_text(), "")

    def test_names_that_differ_only_in_case_are_rejected(self):
        # Windows would set one variable, Linux and macOS two.
        self.assert_rejected(
            json.dumps({"Flag": "1", "FLAG": "2"}), "sets both Flag and FLAG:"
        )

    def test_values_that_are_not_json_objects_are_rejected(self):
        for value in ("[]", '["FLAG=on"]', '"FLAG=on"', "1", "null", "FLAG=on", "{"):
            with self.subTest(value=value):
                self.assert_rejected(
                    value, "must be a JSON object of variable names and string values"
                )

    def test_nothing_is_set_when_validation_fails(self):
        # matrix-exclude is checked last, so env is set only after every check passes.
        result, text = self.validate(
            json.dumps({"FLAG": "on"}), MATRIX_EXCLUDE='[{"os": "nowhere"}]'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("names an os not in operating-systems", result.stderr)
        self.assertEqual(text, "")


RUFF = {"RUFF_VERSION": "0.17.0", "LINT_PATHS": '["."]'}
RUFF_NOTICE = (
    "::notice::Found no ruff.toml, .ruff.toml or pyproject.toml with a [tool.ruff] "
    "table in working-directory or its parents in the checkout{}, so Ruff applies its "
    "default rule set to files that no Ruff configuration covers."
)
MARKDOWN = "Python code blocks in Markdown files"
# A pyproject.toml without a [tool.ruff] table, which Ruff skips.
PROJECT = '[project]\nname = "pkg"\nversion = "1"\n'


class RuffConfigurationNoticeTests(StepHarness, unittest.TestCase):
    python: str

    @classmethod
    def setUpClass(cls):
        # Ruff goes into a venv of its own, so parallel workers never install it into
        # the interpreter running the tests at the same time.
        venv = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, venv, ignore_errors=True)
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        cls.python = str(
            Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )

    def lint(self, files, working_directory=".", env=None):
        return self.execute(
            files,
            {**RUFF, **(env or {})},
            step="Lint with Ruff",
            executable=self.python,
            working_directory=working_directory,
        )

    def assert_notice(self, result, where=""):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("::notice::"), 1, result.stdout)
        self.assertIn(RUFF_NOTICE.format(where), result.stdout)

    def test_notice_without_ruff_configuration(self):
        cases = {
            "pyproject.toml files without [tool.ruff]": {
                "pyproject.toml": "[tool.black]\nline-length = 88\n",
                "pkg/pyproject.toml": PROJECT,
            },
            # tomllib rejects a byte order mark, which Ruff skips.
            "pyproject.toml with a byte order mark": {
                "pkg/pyproject.toml": b"\xef\xbb\xbf" + PROJECT.encode(),
            },
        }
        for case, files in cases.items():
            with self.subTest(case=case):
                result = self.lint({**files, "pkg/app.py": "VALUE = 1\n"}, "pkg")
                self.assert_notice(result)
                self.assertIn(MARKDOWN, result.stdout)

    def test_notice_names_lint_paths_without_configuration(self):
        files = {
            "src/ruff.toml": "",
            "src/app.py": "VALUE = 1\n",
            "tests/test_app.py": "VALUE = 1\n",
        }
        result = self.lint(files, env={"LINT_PATHS": '["src", "tests", "."]'})
        self.assert_notice(result, ', or in or above lint-paths entries ["tests"]')

    def test_notice_leaves_out_markdown_without_ruff_format(self):
        result = self.lint({"app.py": "VALUE = 1\n"}, env={"RUFF_FORMAT": "false"})
        self.assert_notice(result)
        self.assertNotIn(MARKDOWN, result.stdout)

    def test_notice_ignores_configuration_above_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "ruff.toml").write_text("")
            checkout = Path(directory, "checkout")
            checkout.mkdir()
            (checkout / "app.py").write_text("VALUE = 1\n")
            result = self.run_step(checkout, "Lint with Ruff", RUFF, self.python)
        self.assert_notice(result)

    def test_no_notice_with_ruff_configuration(self):
        section = "[tool.ruff]\nline-length = 100\n"
        cases = {
            "ruff.toml": ({"ruff.toml": ""}, ".", {}),
            ".ruff.toml": ({".ruff.toml": ""}, ".", {}),
            "[tool.ruff] in pyproject.toml": ({"pyproject.toml": section}, ".", {}),
            "[tool.ruff] in a pyproject.toml with a byte order mark": (
                {"pyproject.toml": b"\xef\xbb\xbf" + section.encode()},
                ".",
                {},
            ),
            "ruff.toml in a parent": ({"ruff.toml": ""}, "pkg", {}),
            ".ruff.toml in a parent": ({".ruff.toml": ""}, "pkg", {}),
            # Ruff skips a pyproject.toml without [tool.ruff] and keeps looking.
            "[tool.ruff] in a parent's pyproject.toml": (
                {"pyproject.toml": section, "pkg/pyproject.toml": PROJECT},
                "pkg",
                {},
            ),
            # Ruff looks above each file it checks, not only above working-directory.
            "ruff.toml in each lint path": (
                {"pkg/ruff.toml": "", "src/.ruff.toml": "", "src/app.py": ""},
                ".",
                {"LINT_PATHS": '["pkg", "src/app.py"]'},
            ),
        }
        for case, (files, working_directory, env) in cases.items():
            with self.subTest(case=case):
                project = {**files, "pkg/app.py": "VALUE = 1\n"}
                result = self.lint(project, working_directory, env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("::notice::", result.stdout)

    def test_no_notice_for_a_pyproject_toml_ruff_cannot_read(self):
        # Ruff reports the parse error itself.
        result = self.lint({"pyproject.toml": "[project\n", "app.py": "VALUE = 1\n"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to parse", result.stdout + result.stderr)
        self.assertNotIn("::notice::", result.stdout)


PYTEST = {"TEST_RUNNER": "pytest", "PYTEST_VERSION": "9.1.1"}


@unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is not installed")
class PytestRunnerTests(StepHarness, unittest.TestCase):
    def test_pytest_success_failure_and_summary(self):
        tests = (
            "def test_ok():\n    assert True\n\n\ndef test_bad():\n    assert 1 == 2\n"
        )
        with tempfile.TemporaryDirectory() as out:
            summary = Path(out) / "summary.md"
            result = self.execute(
                {"tests/test_mix.py": tests},
                {**PYTEST, "GITHUB_STEP_SUMMARY": str(summary)},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("| 1 | 1 | 0 | 0 |", summary.read_text())
            self.assertIn("test_bad", summary.read_text())
        ok = self.execute(
            {
                "lib.py": "VALUE = 3\n",
                "tests/test_ok.py": "from lib import VALUE\n\n\ndef test_ok():\n    assert VALUE == 3\n",
            },
            PYTEST,
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

    def test_pytest_empty_suite_and_args(self):
        empty = self.execute({"tests/test_none.py": ""}, PYTEST)
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn("No tests discovered", empty.stderr)
        files = {
            "tests/test_marks.py": "import pytest\n\n\n@pytest.mark.slow\ndef test_slow():\n    assert False\n\n\ndef test_fast():\n    pass\n"
        }
        result = self.execute(
            files,
            {
                **PYTEST,
                "PYTEST_ARGS": '-m "not slow" -W ignore::pytest.PytestUnknownMarkWarning',
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_empty_inputs_defer_to_pytest_configuration(self):
        config = '[tool.pytest.ini_options]\ntestpaths = ["src/tests"]\npython_files = ["*_check.py"]\n'
        files = {
            "pyproject.toml": config,
            "src/tests/calc_check.py": "def test_calc():\n    assert 1 + 1 == 2\n",
            "src/tests/other_check.py": "def test_other():\n    pass\n",
            "tests/test_ignored.py": "def test_ignored():\n    assert False\n",
        }
        empty = {**PYTEST, "TEST_DIRECTORY": "", "TEST_PATTERN": ""}
        result = self.execute(files, empty)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("2 passed", result.stdout)
        files["src/tests/calc_check.py"] = "def test_calc():\n    assert 1 + 1 == 3\n"
        self.assertNotEqual(self.execute(files, empty).returncode, 0)

    def test_empty_inputs_collect_pytest_default_file_names(self):
        files = {
            "tests/test_ok.py": "def test_ok():\n    pass\n",
            "tests/foo_test.py": "def test_foo():\n    assert False\n",
        }
        result = self.execute(
            files, {**PYTEST, "TEST_DIRECTORY": "", "TEST_PATTERN": ""}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foo_test.py", result.stdout)
        explicit = self.execute(
            files, {**PYTEST, "TEST_DIRECTORY": "tests", "TEST_PATTERN": "test*.py"}
        )
        self.assertEqual(explicit.returncode, 0, explicit.stdout)

    def test_no_tests_message_names_the_inputs(self):
        result = self.execute(
            {"tests/test_none.py": ""},
            {**PYTEST, "TEST_DIRECTORY": "tests", "TEST_PATTERN": "check_*.py"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "test-directory: 'tests', test-pattern: 'check_*.py'", result.stderr
        )

    def test_pytest_inputs_validated(self):
        for env in (
            {"TEST_RUNNER": "nose"},
            {**PYTEST, "PYTEST_ARGS": '"unterminated'},
            {"PYTEST_ARGS": "-x"},
            {"TEST_RUNNER": "pytest", "PYTEST_VERSION": ""},
        ):
            result = self.execute({"tests/test_ok.py": ""}, env, step="Validate inputs")
            self.assertNotEqual(result.returncode, 0, env)


AUTO = {"TEST_RUNNER": "auto", "PYTEST_VERSION": "9.1.1"}


class AutoRunnerTests(StepHarness, unittest.TestCase):
    """test-runner: auto, and pointing unittest users with pytest tests at pytest."""

    def choose(self, files, env):
        """Run validation; return its test-runner output, log and job summary."""
        with tempfile.TemporaryDirectory() as out:
            output, summary = Path(out) / "output", Path(out) / "summary.md"
            summary.touch()  # as the runner does before each step
            result = self.execute(
                {"tests/test_ok.py": "", **files},
                {
                    **env,
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
                step="Validate inputs",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
            return (
                values["test-runner"],
                result.stdout,
                summary.read_text(encoding="utf-8"),
            )

    def test_auto_picks_pytest_for_each_signal(self):
        pyproject = '[project]\nname = "x"\nversion = "1"\n\n'
        signals = {
            "pytest.toml": ({"pytest.toml": "[pytest]\n"}, {}, "found pytest.toml"),
            ".pytest.toml": ({".pytest.toml": "[pytest]\n"}, {}, "found .pytest.toml"),
            "pytest.ini": ({"pytest.ini": "[pytest]\n"}, {}, "found pytest.ini"),
            ".pytest.ini": ({".pytest.ini": "[pytest]\n"}, {}, "found .pytest.ini"),
            "[tool.pytest]": (
                {"pyproject.toml": pyproject + '[tool.pytest]\nminversion = "9.0"\n'},
                {},
                "found [tool.pytest] in pyproject.toml",
            ),
            "[tool.pytest.ini_options]": (
                {"pyproject.toml": "[tool.pytest.ini_options]\nxfail_strict = true\n"},
                {},
                "found [tool.pytest.ini_options] in pyproject.toml",
            ),
            "tox.ini": (
                {"tox.ini": "[tox]\nenv_list = py3\n\n[pytest]\naddopts = -ra\n"},
                {},
                "found [pytest] in tox.ini",
            ),
            "tox.ini header with a comment": (
                {"tox.ini": "[pytest]  # options for pytest\naddopts = -ra\n"},
                {},
                "found [pytest] in tox.ini",
            ),
            "setup.cfg": (
                {"setup.cfg": "[metadata]\nname = x\n\n[tool:pytest]\naddopts = -ra\n"},
                {},
                "found [tool:pytest] in setup.cfg",
            ),
            "conftest.py in working-directory": (
                {"conftest.py": ""},
                {},
                "found conftest.py",
            ),
            "conftest.py in test-directory": (
                {"checks/conftest.py": ""},
                {"TEST_DIRECTORY": "checks"},
                "found checks/conftest.py",
            ),
            "requirements-file": (
                {
                    "requirements-dev.txt": "iniconfig==2.1.0 \\\n    --hash=sha256:00\n"
                    "PyTest==9.1.1 \\\n    --hash=sha256:01\n"
                },
                {"REQUIREMENTS_FILE": "requirements-dev.txt"},
                "found pytest in requirements-dev.txt",
            ),
            "requirements-file with a byte order mark": (
                {"requirements.txt": "\ufeffpytest==9.1.1\n".encode()},
                {"REQUIREMENTS_FILE": "requirements.txt"},
                "found pytest in requirements.txt",
            ),
            "[project.optional-dependencies]": (
                {
                    "pyproject.toml": pyproject
                    + '[project.optional-dependencies]\ntest = ["pytest>=8"]\n'
                },
                {},
                "found pytest in [project.optional-dependencies] test",
            ),
            "[dependency-groups]": (
                {
                    "pyproject.toml": "[dependency-groups]\n"
                    'dev = [{include-group = "lint"}, "pytest[testing]; python_version >= \'3.11\'"]\n'
                    'lint = ["ruff"]\n'
                },
                {},
                "found pytest in [dependency-groups] dev",
            ),
            "pytest-args": ({}, {"PYTEST_ARGS": "-ra"}, "pytest-args is set"),
        }
        for signal, (files, env, reason) in signals.items():
            with self.subTest(signal=signal):
                runner, log, summary = self.choose(files, {**AUTO, **env})
                self.assertEqual(runner, "pytest")
                line = f"Test runner: pytest (auto: {reason})"
                self.assertIn(line, log)
                self.assertIn(line, summary)

    def test_auto_picks_unittest_without_any_signal(self):
        # Near misses: other pytest-* projects, other tables and tox environments.
        files = {
            "pyproject.toml": (
                '[project]\nname = "x"\nversion = "1"\n\n'
                '[project.optional-dependencies]\ntest = ["pytest-cov"]\n\n'
                '[dependency-groups]\ndev = ["pytest-xdist", {include-group = "lint"}]\n'
                'lint = ["ruff"]\n\n[tool.ruff]\nline-length = 88\n'
            ),
            "tox.ini": "[tox]\nenv_list = py3\n\n[testenv:pytest]\ncommands = pytest\n",
            "setup.cfg": "[metadata]\nname = x\n",
            "requirements.txt": "pytest-xdist==3.8.0\n# pytest==9.1.1\n",
            "tests/test_pytest_style.py": "def test_a():\n    pass\n",
        }
        env = {**AUTO, "REQUIREMENTS_FILE": "requirements.txt", "PYTEST_ARGS": ""}
        runner, log, summary = self.choose(files, env)
        self.assertEqual(runner, "unittest")
        line = (
            "Test runner: unittest (auto: found no pytest configuration, conftest.py, "
            "pytest requirement or pytest-args)"
        )
        self.assertIn(line, log)
        self.assertIn(line, summary)

    def test_an_explicit_runner_is_kept_and_reported(self):
        for env, expected in (
            ({}, "unittest"),
            ({"TEST_RUNNER": "unittest"}, "unittest"),
            (PYTEST, "pytest"),
        ):
            with self.subTest(env=env):
                runner, log, summary = self.choose({"conftest.py": ""}, env)
                self.assertEqual(runner, expected)
                self.assertIn(
                    f"Test runner: {expected} (from the test-runner input)", log
                )
                # Only auto writes to the job summary, so existing summaries stay as is.
                self.assertNotIn("Test runner", summary)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_the_default_stays_unittest(self):
        inputs = yaml.safe_load(WORKFLOW.read_text())[True]["workflow_call"]["inputs"]
        self.assertEqual(inputs["test-runner"]["default"], "unittest")

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_only_validation_reads_the_test_runner_input(self):
        self.assertEqual(
            input_readers("test-runner"),
            [],
            "these must use the validate step's output",
        )
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        steps = {step.get("name"): step for step in jobs["test"]["steps"]}
        resolved = "${{ steps.validate.outputs.test-runner }}"
        for name in (
            "Build and check package",
            "Install test tools",
            "Run tests and reject empty test suites",
        ):
            with self.subTest(step=name):
                self.assertEqual(steps[name]["env"]["TEST_RUNNER"], resolved)
        self.assertIn(
            "steps.validate.outputs.test-runner == 'pytest'",
            steps["Install test tools"]["if"],
        )

    def test_checks_that_depend_on_the_runner_use_the_resolved_one(self):
        config = {"pytest.ini": "[pytest]\n"}
        doctests = {**AUTO, "DOCTEST_PATHS": '["my-scripts/tool.py"]'}
        cases = (
            ({**config}, {**AUTO, "PYTEST_VERSION": ""}, "pytest-version must be"),
            ({}, {**AUTO, "PYTEST_VERSION": ""}, None),
            # Any pytest-args picks pytest, so the pytest-args check cannot fail.
            ({}, {**AUTO, "PYTEST_ARGS": " "}, None),
            # pytest runs doctests in files that unittest cannot import by name.
            ({**config, "my-scripts/tool.py": ""}, doctests, None),
            ({"my-scripts/tool.py": ""}, doctests, "no module name for unittest"),
            (
                {"pyproject.toml": "[tool.pytest\n"},
                AUTO,
                "test-runner: auto could not read pyproject.toml",
            ),
            ({}, {"TEST_RUNNER": "nose"}, "must be unittest, pytest or auto"),
        )
        for files, env, error in cases:
            with self.subTest(files=files, env=env):
                result = self.execute(
                    {"tests/test_ok.py": "", **files}, env, step="Validate inputs"
                )
                if error is None:
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error, result.stderr)

    def test_unittest_points_pytest_style_tests_at_pytest(self):
        # auto is offered only where it would pick pytest; test functions alone are
        # not one of its signals.
        pytest_only = "set test-runner: pytest."
        or_auto = "set test-runner: pytest (or auto)."
        cases = {
            "module-level test function": (
                {"tests/test_a.py": "def test_a():\n    assert 1 == 1\n"},
                {},
                "tests/test_a.py defines test_a()",
                pytest_only,
            ),
            "async test in a directory unittest does not enter": (
                {
                    "tests/test_none.py": "",
                    "tests/unit/test_b.py": "async def test_b():\n    pass\n",
                },
                {},
                "tests/unit/test_b.py defines test_b()",
                pytest_only,
            ),
            "custom test-pattern": (
                {"tests/check_c.py": "def test_c():\n    pass\n"},
                {"TEST_PATTERN": "check_*.py"},
                "tests/check_c.py defines test_c()",
                pytest_only,
            ),
            "pytest configuration": (
                {
                    "tests/test_none.py": "",
                    "pyproject.toml": "[tool.pytest.ini_options]\nxfail_strict = true\n",
                },
                {},
                "found [tool.pytest.ini_options] in pyproject.toml",
                or_auto,
            ),
            "test function and a conftest.py": (
                {"tests/test_a.py": "def test_a():\n    pass\n", "conftest.py": ""},
                {},
                "tests/test_a.py defines test_a()",
                or_auto,
            ),
        }
        for case, (files, env, evidence, advice) in cases.items():
            with self.subTest(case=case):
                result = self.execute(files, {"TEST_RUNNER": "unittest", **env})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("No tests discovered", result.stderr)
                self.assertIn(
                    f"These look like pytest tests ({evidence}); {advice}",
                    result.stderr,
                )
                if advice == pytest_only:
                    self.assertNotIn("(or auto)", result.stderr)

    def test_no_pytest_hint_without_pytest_style_tests(self):
        files = {
            "tests/test_helpers.py": "def helper():\n    pass\n\n\nclass Checks:\n    def test_a(self):\n        pass\n",
            "tests/other.py": "def test_not_matched():\n    pass\n",
            "tests/.hidden/test_d.py": "def test_d():\n    pass\n",
            "tests/__pycache__/test_e.py": "def test_e():\n    pass\n",
            # Installed packages in a virtual environment ship their own tests.
            "tests/venv/pyvenv.cfg": "home = /usr/bin\n",
            "tests/venv/lib/test_f.py": "def test_f():\n    pass\n",
        }
        result = self.execute(files, {"TEST_RUNNER": "unittest"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No tests discovered", result.stderr)
        self.assertNotIn("test-runner: pytest", result.stderr)

    @unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is not installed")
    def test_auto_runs_a_pytest_project_that_fails_with_unittest(self):
        files = {
            "pyproject.toml": "[tool.pytest.ini_options]\nxfail_strict = true\n",
            "tests/test_a.py": "def test_x():\n    assert 1 == 1\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            for name, content in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(content)
            output = Path(directory) / "output"
            validate = self.run_step(
                root, "Validate inputs", {**AUTO, "GITHUB_OUTPUT": str(output)}
            )
            self.assertEqual(validate.returncode, 0, validate.stderr)
            values = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
            tests = "Run tests and reject empty test suites"
            result = self.run_step(root, tests, {"TEST_RUNNER": values["test-runner"]})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1 passed", result.stdout)
            default = self.run_step(root, tests, {"TEST_RUNNER": "unittest"})
            self.assertNotEqual(default.returncode, 0)
            self.assertIn("set test-runner: pytest (or auto)", default.stderr)


class ProjectInstallTests(StepHarness, unittest.TestCase):
    def test_project_inputs_validated(self):
        for env in (
            {"PROJECT_EXTRAS": "test"},
            {"INSTALL_PROJECT": "true", "PROJECT_EXTRAS": "test;rm"},
            {"INSTALL_PROJECT": "true"},
        ):
            result = self.execute({"tests/test_ok.py": ""}, env, step="Validate inputs")
            self.assertNotEqual(result.returncode, 0, env)
        result = self.execute(
            {"tests/test_ok.py": "", "pyproject.toml": ""},
            {"INSTALL_PROJECT": "true", "PROJECT_EXTRAS": "test, dev"},
            step="Validate inputs",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_editable_install_with_extras(self):
        pyproject = (
            '[build-system]\nrequires = ["flit_core>=3.9,<4"]\nbuild-backend = "flit_core.buildapi"\n\n'
            '[project]\nname = "fixture_src"\nversion = "1.0"\ndescription = "Fixture"\n\n'
            "[project.optional-dependencies]\nnothing = []\n"
        )
        files = {"pyproject.toml": pyproject, "fixture_src/__init__.py": "VALUE = 5\n"}
        with tempfile.TemporaryDirectory() as venv:
            subprocess.run([sys.executable, "-m", "venv", venv], check=True)
            python = Path(venv) / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            result = self.execute(
                files,
                {"INSTALL_PROJECT": "true", "PROJECT_EXTRAS": "nothing"},
                step="Install dependencies",
                executable=str(python),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            check = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import importlib.metadata as m; print(m.version('fixture_src'))",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(check.stdout.strip(), "1.0", check.stderr)


UV = {"INSTALLER": "uv", "UV_VERSION": "0.13.4"}


class UvInstallerTests(StepHarness, unittest.TestCase):
    def install_into_venv(self, files, env):
        venv = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, venv, ignore_errors=True)
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        python = Path(venv) / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        result = self.execute(
            files,
            {**UV, **env},
            step="Install dependencies",
            executable=str(python),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return python

    def test_uv_installs_from_uv_lock(self):
        fixture = Path(__file__).parent / "fixtures/uv-project"
        files = {
            name: (fixture / name).read_text() for name in ("pyproject.toml", "uv.lock")
        }
        python = self.install_into_venv(files, {"REQUIREMENTS_FILE": "uv.lock"})
        check = subprocess.run([str(python), "-c", "import iniconfig"], check=False)
        self.assertEqual(check.returncode, 0)

    def test_uv_installs_hashed_requirements(self):
        requirements = (
            "iniconfig==2.1.0 \\\n"
            "    --hash=sha256:9deba5723312380e77435581c6bf4935c94cbfab9b1ed33ef8d238ea168eb760\n"
        )
        python = self.install_into_venv(
            {"requirements.txt": requirements},
            {"REQUIREMENTS_FILE": "requirements.txt"},
        )
        check = subprocess.run([str(python), "-c", "import iniconfig"], check=False)
        self.assertEqual(check.returncode, 0)

    def test_installer_inputs_validated(self):
        cases = (
            ({"INSTALLER": "conda"}, {}, "installer must be pip or uv"),
            (
                {"INSTALLER": "conda", "REQUIREMENTS_FILE": "uv.lock"},
                {"uv.lock": ""},
                "installer must be pip or uv",
            ),
            ({"INSTALLER": "uv", "UV_VERSION": ""}, {}, "uv-version must be"),
            # A lock file chooses uv, so it needs a uv version too.
            ({"REQUIREMENTS_FILE": "uv.lock"}, {"uv.lock": ""}, "uv-version must be"),
        )
        for env, files, error in cases:
            with self.subTest(env=env):
                result = self.execute(
                    {"tests/test_ok.py": "", **files}, env, step="Validate inputs"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
        ok = self.execute(
            {"tests/test_ok.py": "", "uv.lock": ""},
            {**UV, "REQUIREMENTS_FILE": "uv.lock", "PROJECT_EXTRAS": "test"},
            step="Validate inputs",
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def resolve(self, files, env):
        """Run validation; return its installer output, log and job summary."""
        with tempfile.TemporaryDirectory() as out:
            output, summary = Path(out) / "output", Path(out) / "summary.md"
            summary.touch()  # as the runner does before each step
            result = self.execute(
                {"tests/test_ok.py": "", **files},
                {
                    "UV_VERSION": "0.13.4",
                    **env,
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
                step="Validate inputs",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
            return (
                values["installer"],
                result.stdout,
                summary.read_text(encoding="utf-8"),
            )

    def test_lock_files_are_installed_with_uv_whatever_the_installer(self):
        lock_files = {
            "uv.lock": "uv.lock",
            "deps/uv.lock": "uv.lock",
            "pylock.toml": "pylock.toml",
            "pylock.dev.toml": "pylock.dev.toml",
        }
        # The workflow passes the default, pip, when the caller sets no installer.
        for installer in ("pip", "uv"):
            for path, name in lock_files.items():
                with self.subTest(installer=installer, requirements_file=path):
                    chosen, log, summary = self.resolve(
                        {path: ""},
                        {"INSTALLER": installer, "REQUIREMENTS_FILE": path},
                    )
                    self.assertEqual(chosen, "uv")
                    line = f"Installer: uv (requirements-file is {name})"
                    self.assertIn(line, log)
                    self.assertIn(line, summary)

    def test_other_requirements_use_the_installer_input(self):
        requirements = {"requirements.txt": "", "pylock-notes.toml": ""}
        for installer in ("pip", "uv"):
            for name in requirements:
                with self.subTest(installer=installer, requirements_file=name):
                    chosen, log, summary = self.resolve(
                        requirements,
                        {"INSTALLER": installer, "REQUIREMENTS_FILE": name},
                    )
                    self.assertEqual(chosen, installer)
                    line = f"Installer: {installer} (installer input)"
                    self.assertIn(line, log)
                    self.assertIn(line, summary)
        _, _, summary = self.resolve(
            {"pyproject.toml": ""}, {"INSTALLER": "uv", "INSTALL_PROJECT": "true"}
        )
        self.assertIn("Installer: uv (installer input)", summary)
        # With nothing to install the installer is unused, so the summary stays as is.
        chosen, log, summary = self.resolve({}, {"INSTALLER": "pip"})
        self.assertEqual(chosen, "pip")
        self.assertIn("Installer: pip (installer input)", log)
        self.assertNotIn("Installer", summary)

    def test_the_default_installer_installs_a_uv_lock_with_uv(self):
        fixture = Path(__file__).parent / "fixtures/uv-project"
        files = {
            name: (fixture / name).read_text() for name in ("pyproject.toml", "uv.lock")
        }
        # The workflow's defaults for installer and uv-version.
        inputs = {
            "INSTALLER": "pip",
            "UV_VERSION": "0.13.4",
            "REQUIREMENTS_FILE": "uv.lock",
        }
        chosen, _, _ = self.resolve(files, inputs)
        # The install step receives the validate step's installer output.
        python = self.install_into_venv(files, {**inputs, "INSTALLER": chosen})
        for module in ("iniconfig", "uv"):
            check = subprocess.run([str(python), "-c", f"import {module}"], check=False)
            self.assertEqual(check.returncode, 0, module)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_only_validation_reads_the_installer_input(self):
        self.assertEqual(
            input_readers("installer"), [], "these must use the validate step's output"
        )
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        steps = {step.get("name"): step for step in jobs["test"]["steps"]}
        self.assertEqual(
            steps["Validate inputs"]["env"]["INSTALLER"], "${{ inputs.installer }}"
        )
        for name in ("Install dependencies", "Build and check package"):
            with self.subTest(step=name):
                self.assertEqual(
                    steps[name]["env"]["INSTALLER"],
                    "${{ steps.validate.outputs.installer }}",
                )

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_the_default_stays_pip_and_the_uv_example_sets_no_installer(self):
        inputs = yaml.safe_load(WORKFLOW.read_text())[True]["workflow_call"]["inputs"]
        self.assertEqual(inputs["installer"]["default"], "pip")
        example = yaml.safe_load(EXAMPLES.read_text())["jobs"]["uv-project"]["with"]
        self.assertNotIn("installer", example)
        result = self.run_step(
            ROOT,
            "Validate inputs",
            {
                "INSTALLER": inputs["installer"]["default"],
                "UV_VERSION": inputs["uv-version"]["default"],
                "REQUIREMENTS_FILE": example["requirements-file"],
            },
            cwd=ROOT / example["working-directory"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installer: uv (requirements-file is uv.lock)", result.stdout)


class AuditTests(StepHarness, unittest.TestCase):
    def test_audit_inputs_validated(self):
        for env in (
            {"AUDIT_VERSION": "latest"},
            {"AUDIT_IGNORE": "GHSA-1"},
            {"AUDIT_IGNORE": '[""]'},
        ):
            result = self.execute({"tests/test_ok.py": ""}, env, step="Validate inputs")
            self.assertNotEqual(result.returncode, 0, env)

    def test_audit_reports_vulnerable_dependency(self):
        venv = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, venv, ignore_errors=True)
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        python = Path(venv) / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        subprocess.run(
            [str(python), "-m", "pip", "install", "-q", "urllib3==1.26.0"], check=True
        )
        with tempfile.TemporaryDirectory() as out:
            summary = Path(out) / "summary.md"
            result = self.execute(
                {},
                {"AUDIT_VERSION": "2.10.1", "GITHUB_STEP_SUMMARY": str(summary)},
                step="Audit dependencies",
                executable=str(python),
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(summary.exists(), result.stdout + result.stderr)
            self.assertIn("urllib3", summary.read_text(encoding="utf-8"))

    def test_audit_skips_interpreter_bootstrap_packages(self):
        venv = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, venv, ignore_errors=True)
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        python = Path(venv) / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        result = self.execute(
            {},
            {"AUDIT_VERSION": "2.10.1"},
            step="Audit dependencies",
            executable=str(python),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No third-party packages to audit", result.stdout)


class WheelTests(StepHarness, unittest.TestCase):
    PYPROJECT = (
        '[build-system]\nrequires = ["flit_core>=3.9,<4"]\nbuild-backend = "flit_core.buildapi"\n\n'
        '[project]\nname = "wheelpkg"\nversion = "1.0"\ndescription = "Fixture"\nreadme = "README.md"\n'
    )
    TEST = "import unittest\n\nfrom wheelpkg import VALUE\n\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(VALUE, 1)\n"

    def files(self, init):
        return {
            "pyproject.toml": self.PYPROJECT,
            "README.md": "Fixture\n",
            "wheelpkg/__init__.py": init,
            "helper.py": "VALUE = 1\n",
            "tests/test_pkg.py": self.TEST,
        }

    def test_wheel_tests_pass_for_complete_package(self):
        result = self.execute(
            self.files("VALUE = 1\n"),
            {"TEST_WHEEL": "true"},
            step="Build and check package",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("in a clean environment", result.stdout)

    def test_wheel_tests_catch_modules_missing_from_the_wheel(self):
        # helper.py sits beside the package, so it is importable from the source tree
        # but is not part of the wheel.
        result = self.execute(
            self.files("from helper import VALUE\n"),
            {"TEST_WHEEL": "true"},
            step="Build and check package",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("helper", result.stderr)

    def test_build_leaves_one_sdist_and_one_wheel_for_upload(self):
        with tempfile.TemporaryDirectory() as runner_temp:
            result = self.execute(
                self.files("VALUE = 1\n"),
                {"RUNNER_TEMP": runner_temp},
                step="Build and check package",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            names = sorted(
                p.name for p in (Path(runner_temp) / "python-ci-dist").iterdir()
            )
            self.assertEqual(len(names), 2, names)
            self.assertTrue(
                names[0].endswith(".tar.gz") or names[1].endswith(".tar.gz"), names
            )
            self.assertTrue(any(name.endswith(".whl") for name in names), names)

    def test_dist_upload_runs_only_after_a_successful_build_on_the_primary_cell(self):
        text = WORKFLOW.read_text()
        block = text[text.index("- name: Upload distributions") :].split(
            "\n      - ", 1
        )[0]
        # Opt-in, so existing callers that upload their own "dist" do not collide.
        self.assertIn(
            "if: inputs.upload-dist && steps.validate.outputs.primary == 'true'",
            block,
        )
        self.assertNotIn("always()", block)
        self.assertIn("path: ${{ runner.temp }}/python-ci-dist", block)
        self.assertIn("name: dist${{ steps.validate.outputs.artifact-suffix }}", block)

    def test_wheel_tests_and_dist_upload_require_build(self):
        for name, label in (
            ("TEST_WHEEL", "test-wheel"),
            ("UPLOAD_DIST", "upload-dist"),
        ):
            with self.subTest(name=name):
                result = self.execute(
                    {"tests/test_ok.py": ""}, {name: "true"}, step="Validate inputs"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{label} requires build-package: true", result.stderr)

    def venv_with(self, *packages):
        venv = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, venv, ignore_errors=True)
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        python = Path(venv) / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        if packages:
            subprocess.run(
                [str(python), "-m", "pip", "install", "-q", *packages], check=True
            )
        return str(python)

    def test_wheel_tests_use_the_locked_dependency_versions(self):
        fixture = Path(__file__).parent / "fixtures/wheel-lock-project"
        files = {
            name: (fixture / name).read_text() for name in ("pyproject.toml", "uv.lock")
        }
        files.update(
            {
                "README.md": "Fixture\n",
                "lockedpkg/__init__.py": "import iniconfig\n",
                "tests/test_locked.py": (
                    "import importlib.metadata\nimport unittest\n\nimport lockedpkg\n\n\n"
                    "class T(unittest.TestCase):\n    def test_lock(self):\n"
                    "        self.assertEqual(importlib.metadata.version('iniconfig'), '2.1.0')\n"
                ),
            }
        )
        env = {"REQUIREMENTS_FILE": "uv.lock", "INSTALLER": "uv", "TEST_WHEEL": "true"}
        result = self.execute(
            files,
            env,
            step="Build and check package",
            executable=self.venv_with("uv==0.13.4"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Testing lockedpkg-1.0", result.stdout)

    def test_wheel_replaces_a_copy_of_the_project_from_the_lock(self):
        # pip lock writes the project itself as a directory entry. The tests must still
        # import the built wheel, not the copy installed from the source tree.
        files = self.files("VALUE = 1\n")
        files["pylock.toml"] = (
            'lock-version = "1.0"\ncreated-by = "pip"\n\n'
            '[[packages]]\nname = "wheelpkg"\ndirectory = { path = "." }\n'
        )
        files["tests/test_source.py"] = (
            "import importlib.metadata\nimport json\nimport unittest\n\n\n"
            "class T(unittest.TestCase):\n    def test_from_wheel(self):\n"
            "        dist = importlib.metadata.distribution('wheelpkg')\n"
            "        url = json.loads(dist.read_text('direct_url.json'))['url']\n"
            "        self.assertTrue(url.endswith('.whl'), url)\n"
        )
        env = {
            "REQUIREMENTS_FILE": "pylock.toml",
            "INSTALLER": "uv",
            "TEST_WHEEL": "true",
        }
        result = self.execute(
            files,
            env,
            step="Build and check package",
            executable=self.venv_with("uv==0.13.4"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wheel_tests_honour_require_hashes_false_for_a_uv_lock(self):
        # A path source exports without a hash, so only an unhashed install works.
        fixture = Path(__file__).parent / "fixtures/wheel-path-lock"
        # Only the sources: CI's compile step leaves __pycache__ in the fixture.
        files = {
            path.relative_to(fixture).as_posix(): path.read_text()
            for path in fixture.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        files.update(
            {
                "README.md": "Fixture\n",
                "pathpkg/__init__.py": "from localdep import VALUE\n",
                "tests/test_path.py": (
                    "import unittest\n\nfrom pathpkg import VALUE\n\n\n"
                    "class T(unittest.TestCase):\n    def test_value(self):\n"
                    "        self.assertEqual(VALUE, 1)\n"
                ),
            }
        )
        env = {
            "REQUIREMENTS_FILE": "uv.lock",
            "INSTALLER": "uv",
            "REQUIRE_HASHES": "false",
            "TEST_WHEEL": "true",
        }
        result = self.execute(
            files,
            env,
            step="Build and check package",
            executable=self.venv_with("uv==0.13.4"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Testing pathpkg-1.0", result.stdout)

    def test_wheel_tests_use_pytest_version_over_a_locked_pytest(self):
        files = self.files("VALUE = 1\n")
        files["requirements.txt"] = "pytest==8.4.2\n"
        files["tests/test_pkg.py"] = (
            "import pytest\n\n\ndef test_version():\n"
            "    assert pytest.__version__ == '9.1.1'\n"
        )
        env = {
            "REQUIREMENTS_FILE": "requirements.txt",
            "REQUIRE_HASHES": "false",
            "TEST_WHEEL": "true",
            **PYTEST,
        }
        result = self.execute(files, env, step="Build and check package")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wheel_tests_fail_when_the_lock_misses_a_dependency(self):
        files = self.files("VALUE = 1\n")
        files["pyproject.toml"] = files["pyproject.toml"].replace(
            'readme = "README.md"\n',
            'readme = "README.md"\ndependencies = ["iniconfig"]\n',
        )
        files["requirements.txt"] = "# nothing locked\n"
        env = {"REQUIREMENTS_FILE": "requirements.txt", "TEST_WHEEL": "true"}
        result = self.execute(files, env, step="Build and check package")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not satisfied by requirements-file", result.stderr)

    def test_wheel_requires_build(self):
        result = self.execute(
            {"tests/test_ok.py": ""}, {"TEST_WHEEL": "true"}, step="Validate inputs"
        )
        self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_readme_says_the_wheel_test_runs_once_on_the_primary_cell(self):
        # The wheel test runs inside the build step, which only the primary cell runs.
        text = WORKFLOW.read_text()
        build = text[text.index("- name: Build and check package") :]
        build = build.split("\n      - ", 1)[0]
        self.assertIn("steps.validate.outputs.primary == 'true'", build)
        self.assertIn("TEST_WHEEL: ${{ inputs.test-wheel }}", build)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("build-package", "test-wheel"):
            (row,) = re.findall(rf"^\| `{name}` \|.*$", readme, re.MULTILINE)
            self.assertIn("once per run on the primary cell", row.lower(), name)
        primary = re.search(r"(?ms)^### The primary cell\n(.*?)^#", readme).group(1)
        self.assertIn("the wheel test", " ".join(primary.split()))
        steps = re.search(r"(?ms)^## How it runs\n(.*?)^## ", readme).group(1)
        (item,) = re.findall(r"(?ms)^\d+\. Build and check the package.*?^\d", steps)
        self.assertIn("once per run on the primary cell", " ".join(item.split()))
        # Each example that tests its wheel names that cell, the only one that does.
        defaults = yaml.safe_load(text)[True]["workflow_call"]["inputs"]
        jobs = yaml.safe_load((ROOT / ".github/workflows/examples.yml").read_text())
        names = {"ubuntu": "Linux", "windows": "Windows", "macos": "macOS"}
        for name, job in jobs["jobs"].items():
            inputs = {key: value.get("default") for key, value in defaults.items()}
            inputs.update(job["with"])
            if not inputs["test-wheel"]:
                continue
            system = json.loads(inputs["operating-systems"])[0].split("-")[0]
            experimental = json.loads(inputs["experimental-python-versions"])
            python = next(
                version
                for version in json.loads(inputs["python-versions"])
                if version not in experimental
            )
            bullet = re.search(rf"(?ms)^- `{name}`: (.*?)(?=^- |^$)", readme)
            self.assertIsNotNone(bullet, name)
            words = " ".join(bullet.group(1).split())
            self.assertIn(
                f"once, on the primary cell ({names[system]}, Python {python})", words
            )
            # Nothing said about the wheel names another operating system.
            wheel = words[words.index("wheel") :]
            for other in sorted(set(names.values()) - {names[system]}):
                self.assertNotIn(other, wheel, name)


class DoctestAndTimingTests(StepHarness, unittest.TestCase):
    MODULE = (
        'def double(x):\n    """\n    >>> double(2)\n    4\n    """\n    return x * 2\n'
    )

    def test_doctests_run_with_unittest(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/maths.py": self.MODULE,
            "tests/test_none.py": "",
        }
        ok = self.execute(files, {"DOCTEST_PATHS": '["pkg"]'})
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("Ran 1 test", ok.stderr)
        files["pkg/maths.py"] = self.MODULE.replace("    4\n", "    5\n")
        bad = self.execute(files, {"DOCTEST_PATHS": '["pkg"]'})
        self.assertNotEqual(bad.returncode, 0)

    @unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is not installed")
    def test_doctests_run_with_pytest(self):
        files = {
            "maths.py": self.MODULE.replace("    4\n", "    5\n"),
            "tests/test_ok.py": "def test_ok():\n    pass\n",
        }
        result = self.execute(files, {**PYTEST, "DOCTEST_PATHS": '["maths.py"]'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("maths.double", result.stdout)

    def test_doctest_paths_validated(self):
        result = self.execute(
            {"tests/test_ok.py": ""},
            {"DOCTEST_PATHS": '["missing"]'},
            step="Validate inputs",
        )
        self.assertNotEqual(result.returncode, 0)

    def test_malformed_doctest_is_an_error_naming_the_file(self):
        import xml.etree.ElementTree as ET

        files = {
            "pkg/__init__.py": "",
            "pkg/tools.py": 'def f():\n    """\n    >>>f()\n    """\n',
            "tests/test_ok.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
        }
        with tempfile.TemporaryDirectory() as out:
            junit = Path(out) / "junit.xml"
            result = self.execute(
                files, {"DOCTEST_PATHS": '["pkg"]', "JUNIT_FILE": str(junit)}
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Malformed doctest in pkg/tools.py", result.stderr)
            root = ET.parse(junit).getroot()
            self.assertEqual(root.get("tests"), "2")
            self.assertEqual(root.get("errors"), "1")
            case = next(c for c in root.iter("testcase") if c.find("error") is not None)
            self.assertEqual(
                (case.get("classname"), case.get("name")), ("pkg.tools", "doctests")
            )

    def test_doctest_paths_that_cannot_run_are_rejected_by_name(self):
        cases = (
            (
                {"my-scripts/tool.py": ""},
                '["my-scripts/tool.py"]',
                "has no module name for unittest to import",
            ),
            ({"notes.txt": ""}, '["notes.txt"]', "is not a .py file"),
        )
        for files, paths, message in cases:
            with self.subTest(paths=paths):
                result = self.execute(
                    {"tests/test_ok.py": "", **files},
                    {"DOCTEST_PATHS": paths},
                    step="Validate inputs",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_directory_entries_warn_about_files_they_cannot_import(self):
        files = {
            "tests/test_ok.py": "",
            "pkg/mod.py": "",
            "my-scripts/tool.py": "",
            ".github/scripts/release.py": "",
        }
        result = self.execute(files, {"DOCTEST_PATHS": '["."]'}, step="Validate inputs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning::doctest-paths entry '.' skips files", result.stdout)
        self.assertIn("my-scripts/tool.py", result.stdout)
        self.assertNotIn("pkg/mod.py", result.stdout)

    def test_pytest_accepts_doctest_files_without_module_names(self):
        files = {
            "tests/test_ok.py": "",
            "my-scripts/build-docs.py": "",
            ".github/scripts/release.py": "",
        }
        for paths in ('["my-scripts/build-docs.py"]', '["."]'):
            with self.subTest(paths=paths):
                result = self.execute(
                    files,
                    {**PYTEST, "DOCTEST_PATHS": paths},
                    step="Validate inputs",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("::warning::doctest-paths", result.stdout)

    def test_doctest_paths_outside_working_directory_are_rejected(self):
        files = {"app/tests/test_ok.py": "", "shared/util.py": ""}
        result = self.execute(
            files,
            {"DOCTEST_PATHS": '["../shared/util.py"]'},
            step="Validate inputs",
            working_directory="app",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside working-directory", result.stderr)

    def test_inputs_without_effect_warn(self):
        cases = (
            (
                {"COVERAGE_FAIL_UNDER": "80", "COVERAGE_VERSION": ""},
                {},
                "coverage-fail-under has no effect without coverage-version",
            ),
            (
                {"AUDIT_IGNORE": '["GHSA-1"]', "AUDIT_VERSION": ""},
                {},
                "audit-ignore has no effect without audit-version",
            ),
            (
                {
                    "INSTALL_PROJECT": "true",
                    "PROJECT_EXTRAS": "test",
                    "REQUIREMENTS_FILE": "requirements.txt",
                },
                {"pyproject.toml": "", "requirements.txt": ""},
                "extras' dependencies must already be in requirements-file",
            ),
        )
        for env, files, warning in cases:
            with self.subTest(warning=warning):
                result = self.execute(
                    {"tests/test_ok.py": "", **files}, env, step="Validate inputs"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("::warning::", result.stdout)
                self.assertIn(warning, result.stdout)
        quiet = self.execute(
            {"tests/test_ok.py": ""},
            {"COVERAGE_FAIL_UNDER": "0"},
            step="Validate inputs",
        )
        self.assertNotIn("::warning::", quiet.stdout)

    def test_failed_hashed_install_explains_require_hashes(self):
        result = self.execute(
            {"requirements.txt": "./local\n", "local/pyproject.toml": ""},
            {"REQUIREMENTS_FILE": "requirements.txt", "PIP_NO_INDEX": "1"},
            step="Install dependencies",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "::error::Installing requirements-file failed with require-hashes: true",
            result.stdout,
        )

    def test_python_versions_below_the_floor_warn(self):
        result = self.execute(
            {"tests/test_ok.py": ""},
            {"PYTHON_VERSIONS": '["3.10", "pypy3.10", "3.13"]'},
            step="Validate inputs",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning::python-versions lists 3.10", result.stdout)
        self.assertIn("::warning::python-versions lists pypy3.10", result.stdout)

    def test_summary_lists_slowest_tests(self):
        tests = "import time, unittest\n\n\nclass T(unittest.TestCase):\n    def test_slow(self):\n        time.sleep(0.2)\n\n    def test_fast(self):\n        pass\n"
        with tempfile.TemporaryDirectory() as out:
            summary = Path(out) / "summary.md"
            result = self.execute(
                {"tests/test_t.py": tests}, {"GITHUB_STEP_SUMMARY": str(summary)}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            text = summary.read_text(encoding="utf-8")
            self.assertIn("2 slowest tests", text)
            self.assertLess(text.index("test_slow"), text.index("test_fast"))


# A status function in an if: replaces the implicit success() check.
STATUS_FUNCTION = re.compile(r"\b(?:always|success|failure|cancelled)\(\s*\)")


def expression(condition):
    """Return the expression of an if: value, with or without its ${{ }}."""
    match = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", condition.strip(), re.DOTALL)
    return match.group(1) if match else condition.strip()


class CheckSummaryTests(StepHarness, unittest.TestCase):
    """The tests run after failed checks, and a final table records every check."""

    def job_steps(self):
        return yaml.safe_load(WORKFLOW.read_text())["jobs"]["test"]["steps"]

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_tests_depend_only_on_validation_the_hook_and_installation(self):
        steps = {step.get("name"): step for step in self.job_steps()}
        gate = (
            "!cancelled() && steps.validate.outcome == 'success' && "
            "steps.pre-test.outcome != 'failure' && "
            "steps.install.outcome != 'failure'"
        )
        # A failed pre-test hook skips the installation through its implicit
        # success(), which leaves the installation's outcome at skipped, so the test
        # tools and the tests check the hook's outcome themselves.
        install = steps["Install dependencies"]["if"]
        self.assertIsNone(STATUS_FUNCTION.search(install), install)
        tools = expression(steps["Install test tools"]["if"])
        self.assertTrue(tools.startswith(gate + " && "), tools)
        self.assertIn(
            "(inputs.coverage-version != '' || "
            "steps.validate.outputs.test-runner == 'pytest')",
            tools,
        )
        # Neither audit, Ruff, mypy, the syntax check nor the build gates the tests.
        tests = expression(steps["Run tests and reject empty test suites"]["if"])
        self.assertEqual(tests, gate + " && steps.test-tools.outcome != 'failure'")
        # A failed check skips the build, and the implicit success() then keeps the
        # upload from publishing anything.
        upload = steps["Upload distributions"]["if"]
        self.assertIsNone(STATUS_FUNCTION.search(upload), upload)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_the_summary_step_runs_last_with_every_outcome(self):
        steps = self.job_steps()
        last = steps[-1]
        self.assertEqual(last["name"], "Summarise checks")
        self.assertEqual(
            last["if"], "${{ always() && steps.validate.outcome != 'skipped' }}"
        )
        self.assertEqual(script("Summarise checks"), "checks")
        # A working-directory that does not exist fails validation; the table, which
        # reads no project files, must not fail with it.
        self.assertEqual(last["working-directory"], "${{ github.workspace }}")
        ids = {step["name"]: step["id"] for step in steps if "id" in step}
        self.assertEqual(
            ids,
            {
                "Validate inputs": "validate",
                "Run the pre-test hook": "pre-test",
                "Install dependencies": "install",
                "Audit dependencies": "audit",
                "Lint with Ruff": "ruff",
                "Type-check with mypy": "mypy",
                "Check Python syntax": "compile",
                "Build and check package": "build",
                "Install test tools": "test-tools",
                "Run tests and reject empty test suites": "test",
            },
        )
        # An outcome of a step id that does not exist would be empty, not an error.
        outcomes = sorted(
            value for name, value in last["env"].items() if name.endswith("_OUTCOME")
        )
        self.assertEqual(
            outcomes, sorted(f"${{{{ steps.{id_}.outcome }}}}" for id_ in ids.values())
        )

    def summary_rows(self, env, files=None):
        """Run the summary step and return its table rows as (check, outcome, version)."""
        with tempfile.TemporaryDirectory() as out:
            summary = Path(out) / "summary.md"
            result = self.execute(
                files or {},
                {**env, "GITHUB_STEP_SUMMARY": str(summary)},
                step="Summarise checks",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            text = summary.read_text(encoding="utf-8")
        self.assertIn(text.strip(), result.stdout)
        self.assertEqual(text.count("| Check | Outcome | Version |"), 1)
        lines = text[text.index("| Check |") :].splitlines()[2:]
        rows = []
        for line in lines:
            if not line.startswith("|"):
                break
            rows.append(tuple(cell.strip() for cell in line.strip("|").split("|")))
        return rows

    def test_the_table_has_one_row_per_check(self):
        rows = self.summary_rows(
            {
                "VALIDATE_OUTCOME": "success",
                "PRE_TEST_OUTCOME": "success",
                "INSTALL_OUTCOME": "success",
                "REQUIREMENTS_FILE": "requirements.txt",
                "INSTALLER": "uv",
                "UV_VERSION": "0.13.4",
                "AUDIT_OUTCOME": "success",
                "AUDIT_VERSION": "2.10.1",
                "RUFF_OUTCOME": "success",
                "RUFF_VERSION": "0.17.0",
                "MYPY_OUTCOME": "failure",
                "MYPY_VERSION": "2.4.0",
                "COMPILE_OUTCOME": "skipped",
                "BUILD_OUTCOME": "skipped",
                "BUILD_PACKAGE": "true",
                "PRIMARY": "true",
                "TEST_TOOLS_OUTCOME": "success",
                "TEST_RUNNER": "pytest",
                "PYTEST_VERSION": "9.1.1",
                "COVERAGE_VERSION": "7.16.2",
                "TEST_OUTCOME": "success",
            }
        )
        test_tools = "pytest 9.1.1, coverage 7.16.2"
        self.assertEqual(
            rows,
            [
                ("Validate inputs", "success", ""),
                ("Run the pre-test hook", "success", ""),
                ("Install dependencies", "success", "uv 0.13.4"),
                ("Audit dependencies", "success", "pip-audit 2.10.1"),
                ("Lint with Ruff", "success", "Ruff 0.17.0"),
                ("Type-check with mypy", "failure", "mypy 2.4.0"),
                ("Check Python syntax", "skipped", ""),
                ("Build and check package", "skipped", ""),
                ("Install test tools", "success", test_tools),
                ("Run tests and reject empty test suites", "success", test_tools),
            ],
        )

    def test_disabled_and_once_per_run_checks_are_told_apart(self):
        skipped = {
            name: "skipped"
            for name in (
                "PRE_TEST_OUTCOME",
                "INSTALL_OUTCOME",
                "AUDIT_OUTCOME",
                "RUFF_OUTCOME",
                "MYPY_OUTCOME",
                "BUILD_OUTCOME",
                "TEST_TOOLS_OUTCOME",
            )
        }
        # A cell other than the primary one, with Ruff and the build enabled.
        rows = self.summary_rows(
            {
                **skipped,
                "VALIDATE_OUTCOME": "success",
                "COMPILE_OUTCOME": "success",
                "TEST_OUTCOME": "failure",
                "PRIMARY": "false",
                "RUFF_VERSION": "0.17.0",
                "BUILD_PACKAGE": "true",
                "INSTALLER": "pip",
                "TEST_RUNNER": "unittest",
                "PYTEST_VERSION": "9.1.1",
            }
        )
        once = "skipped (runs on the primary cell)"
        self.assertEqual(
            rows,
            [
                ("Validate inputs", "success", ""),
                ("Run the pre-test hook", "not enabled", ""),
                ("Install dependencies", "not enabled", ""),
                ("Audit dependencies", "not enabled", ""),
                ("Lint with Ruff", once, "Ruff 0.17.0"),
                ("Type-check with mypy", "not enabled", ""),
                ("Check Python syntax", "success", ""),
                ("Build and check package", once, ""),
                ("Install test tools", "not enabled", ""),
                ("Run tests and reject empty test suites", "failure", ""),
            ],
        )
        # When validation fails, nothing else runs and no runner is resolved.
        rows = self.summary_rows(
            {
                **skipped,
                "VALIDATE_OUTCOME": "failure",
                "COMPILE_OUTCOME": "skipped",
                "TEST_OUTCOME": "skipped",
                "MYPY_VERSION": "2.4.0",
                "PYTEST_VERSION": "9.1.1",
            }
        )
        self.assertEqual(
            rows,
            [
                ("Validate inputs", "failure", ""),
                ("Run the pre-test hook", "not enabled", ""),
                ("Install dependencies", "not enabled", ""),
                ("Audit dependencies", "not enabled", ""),
                ("Lint with Ruff", "not enabled", ""),
                ("Type-check with mypy", "skipped", "mypy 2.4.0"),
                ("Check Python syntax", "skipped", ""),
                ("Build and check package", "not enabled", ""),
                ("Install test tools", "skipped", ""),
                ("Run tests and reject empty test suites", "skipped", ""),
            ],
        )

    def test_the_hook_row_reads_not_enabled_only_without_a_hook(self):
        hook = ("Run the pre-test hook",)
        # After a failed validation, the hook is skipped whether or not it exists.
        failed = {"VALIDATE_OUTCOME": "failure", "PRE_TEST_OUTCOME": "skipped"}
        for files, outcome in (({}, "not enabled"), ({PRE_TEST_HOOK: ""}, "skipped")):
            with self.subTest(outcome=outcome):
                rows = self.summary_rows(failed, files)
                self.assertEqual(rows[1], (*hook, outcome, ""))
        # A failed hook skips the installation through its implicit success().
        rows = self.summary_rows(
            {
                "VALIDATE_OUTCOME": "success",
                "PRE_TEST_OUTCOME": "failure",
                "INSTALL_OUTCOME": "skipped",
                "REQUIREMENTS_FILE": "requirements.txt",
            },
            {PRE_TEST_HOOK: ""},
        )
        self.assertEqual(
            rows[1:3],
            [(*hook, "failure", ""), ("Install dependencies", "skipped", "")],
        )

    def test_a_version_the_console_cannot_encode_still_gets_a_table(self):
        # Windows runners print through the ANSI code page, and a failed validation
        # leaves the versions unchecked.
        with tempfile.TemporaryDirectory() as out:
            summary = Path(out) / "summary.md"
            result = self.execute(
                {},
                {
                    "PYTHONIOENCODING": "cp1252",
                    "GITHUB_STEP_SUMMARY": str(summary),
                    "VALIDATE_OUTCOME": "failure",
                    "MYPY_OUTCOME": "skipped",
                    "MYPY_VERSION": "\u22651.11",
                },
                step="Summarise checks",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            text = summary.read_text(encoding="utf-8")
        self.assertIn("| Type-check with mypy | skipped | mypy \u22651.11 |", text)
        self.assertIn("| Type-check with mypy | skipped | mypy ?1.11 |", result.stdout)


class PreTestHookTests(unittest.TestCase):
    """The caller's own action adds the setup a calling job cannot add as steps."""

    def hook(self):
        """Return the test job's step names and the step that runs the hook."""
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["test"]["steps"]
        names = [step.get("name") for step in steps]
        return names, steps[names.index("Run the pre-test hook")]

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_the_hook_runs_after_validation_and_before_installation(self):
        names, step = self.hook()
        # After validation, so bad configuration fails before any caller code runs,
        # and before installation, so system libraries are there for sdist builds.
        index = names.index("Run the pre-test hook")
        self.assertEqual(
            names[index - 1 : index + 2],
            ["Validate inputs", "Run the pre-test hook", "Install dependencies"],
        )
        self.assertEqual(step["id"], "pre-test")
        # Only when the caller has a hook. No status function, so the implicit
        # success() skips it after a failed validation.
        self.assertEqual(step["if"], f"hashFiles('{PRE_TEST_HOOK}') != ''")
        # "./" resolves in the caller's checkout, where hashFiles looked.
        self.assertEqual(step["uses"], "./.github/actions/python-ci-pre-test")
        self.assertEqual(
            step["with"],
            {
                "python-version": "${{ matrix.python-version }}",
                "os": "${{ toJSON(matrix.os) }}",
                "working-directory": "${{ inputs.working-directory }}",
                "call-name": "${{ inputs.call-name }}",
            },
        )
        self.assertEqual(load_steps().PRE_TEST_HOOK, PRE_TEST_HOOK)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_this_repositorys_hook_declares_every_input_it_is_given(self):
        # The runner warns about an input that the action does not declare.
        _, step = self.hook()
        action = yaml.safe_load((ROOT / PRE_TEST_HOOK).read_text(encoding="utf-8"))
        self.assertEqual(action["runs"]["using"], "composite")
        self.assertEqual(set(action["inputs"]), set(step["with"]))

    @unittest.skipUnless(
        os.environ.get("GITHUB_ACTIONS") == "true", "only GitHub Actions runs the hook"
    )
    def test_this_repositorys_hook_ran_before_the_tests(self):
        # The hook sets this to the cell's python-versions entry, such as 3.14t, on
        # every cell of the smoke job.
        running = f"{sys.version_info.major}.{sys.version_info.minor}"
        if sysconfig.get_config_var("Py_GIL_DISABLED"):
            running += "t"
        self.assertEqual(os.environ.get("PYTHON_CI_PRE_TEST_PYTHON"), running)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_the_hook_is_documented(self):
        _, step = self.hook()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = re.search(r"(?ms)^### Adding setup steps\n(.*?)^#", readme)
        self.assertIsNotNone(section)
        words = " ".join(section.group(1).split())
        phrases = [f"`{PRE_TEST_HOOK}`", "`runner.os`", "`GITHUB_ENV`", "`GITHUB_PATH`"]
        phrases += [f"- `{name}`:" for name in step["with"]]
        phrases += ["apt-get install", "brew install", "choco install"]
        phrases += ["inputs.working-directory", "](SECURITY.md)"]
        # Unlike test code, a hook can restore an Actions cache, which would undo
        # the no-cache rule of a call with upload-dist.
        phrases += ["`actions/cache`", "`upload-dist`"]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, words)
        steps = markdown_section("README.md", "How it runs")
        self.assertIn("](#adding-setup-steps)", steps)
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        bullet = re.search(r"(?ms)^- The pre-test hook, (.*?)(?=^- |^#|\Z)", security)
        self.assertIsNotNone(bullet)
        words = " ".join(bullet.group(1).split())
        for phrase in (
            f"`{PRE_TEST_HOOK}`",
            "same trust as your test code",
            "`github.token`",
            "Actions caches",
            "`upload-dist`",
            "`cache-mode`",
        ):
            with self.subTest(file="SECURITY.md", phrase=phrase):
                self.assertIn(phrase, words)
        interface = " ".join(
            markdown_section("CONTRIBUTING.md", "Public interface").split()
        )
        self.assertIn(f"`{PRE_TEST_HOOK}`", interface)


def load_steps():
    spec = importlib.util.spec_from_file_location("python_ci_steps", STEPS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VenvPythonTests(unittest.TestCase):
    def test_the_interpreter_path_names_an_existing_file(self):
        import venv

        steps = load_steps()
        with tempfile.TemporaryDirectory() as directory:
            venv.EnvBuilder(with_pip=False).create(directory)
            self.assertTrue(os.path.isfile(steps.venv_python(directory)))


class UnmetRequirementsTests(unittest.TestCase):
    def test_only_the_wheels_own_requirements_count(self):
        steps = load_steps()
        report = (
            "flake8 7.0.0 has requirement pycodestyle<2.12.0,>=2.11.0, but you have "
            "pycodestyle 2.12.1.\n"
            "My_Pkg 1.0 requires iniconfig, which is not installed.\n"
        )
        done = subprocess.CompletedProcess([], 1, stdout=report, stderr="")
        with unittest.mock.patch.object(steps.subprocess, "run", return_value=done):
            unmet = steps.unmet_requirements(
                "python", "dist/my_pkg-1.0-py3-none-any.whl"
            )
        self.assertEqual(
            unmet, ["My_Pkg 1.0 requires iniconfig, which is not installed."]
        )


class RecordingResultTests(unittest.TestCase):
    def run_suite(self, steps, case):
        import io

        suite = unittest.TestSuite([case])
        runner = unittest.TextTestRunner(
            stream=io.StringIO(), resultclass=steps.RecordingResult
        )
        return runner.run(suite)

    def test_records_hold_ids_so_tests_can_be_collected(self):
        import gc
        import weakref

        steps = load_steps()

        class Case(unittest.TestCase):
            def test_ok(self):
                pass

        case = Case("test_ok")
        ref = weakref.ref(case)
        result = self.run_suite(steps, case)
        del case
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(result.records[0].test_id.rsplit(".", 1)[1], "test_ok")

    def test_each_run_keeps_its_own_records(self):
        steps = load_steps()

        class Case(unittest.TestCase):
            def test_ok(self):
                pass

        first = self.run_suite(steps, Case("test_ok"))
        second = self.run_suite(steps, Case("test_ok"))
        self.assertEqual((len(first.records), len(second.records)), (1, 1))


class HarnessIsolationTests(StepHarness, unittest.TestCase):
    def test_input_variables_come_from_the_workflow(self):
        for name in (
            "TEST_DIRECTORY",
            "COVERAGE_VERSION",
            "JUNIT_FILE",
            "TEST_RUNNER",
            "PYTEST_ARGS",
            "PRIMARY",
            "TEST_OUTCOME",
        ):
            self.assertIn(name, INPUT_VARIABLES)
        self.assertNotIn("PATH", INPUT_VARIABLES)

    def test_outer_input_variables_do_not_reach_inner_runs(self):
        files = {
            "tests/test_ok.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
        }
        leaked = {
            "TEST_RUNNER": "pytest",
            "PYTEST_VERSION": "9.1.1",
            "TEST_PATTERN": "nothing_*.py",
            "COVERAGE_VERSION": "9.9",
        }
        with unittest.mock.patch.dict(os.environ, leaked):
            result = self.execute(files)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def test_a_missing_runner_temp_directory_is_created(self):
        steps = load_steps()
        with tempfile.TemporaryDirectory() as parent:
            missing = os.path.join(parent, "runner", "temp")
            with unittest.mock.patch.dict(os.environ, {"RUNNER_TEMP": missing}):
                scratch = steps.scratch_dir("x-")
            self.assertEqual(os.path.dirname(scratch), missing)
            self.assertTrue(os.path.isdir(scratch))


def locked_versions(path=DEV_REQUIREMENTS):
    """Return the versions a requirements file pins, by canonical project name."""
    pins = re.findall(
        r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)",
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return {re.sub(r"[-_.]+", "-", name).lower(): version for name, version in pins}


class ParallelSelfTestTests(unittest.TestCase):
    """This repository's CI runs these tests in parallel pytest-xdist workers."""

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_the_smoke_test_runs_the_suite_in_parallel_workers(self):
        workflow = yaml.safe_load(SMOKE.read_text(encoding="utf-8"))
        inputs = workflow["jobs"]["smoke"]["with"]
        self.assertEqual(inputs.get("test-runner"), "pytest")
        # Read the worker count the way pytest's parser does, so that -n4,
        # --numprocesses=4 and -n auto count as well as -n 4.
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        parser.add_argument("-n", "--numprocesses")
        args = shlex.split(inputs.get("pytest-args", ""))
        workers = parser.parse_known_args(args)[0].numprocesses
        self.assertIsNotNone(workers, "pytest-args does not set -n")
        if workers not in ("auto", "logical"):
            self.assertGreater(int(workers), 1)
        # -n comes from pytest-xdist, which the smoke job installs from the lock.
        self.assertEqual(inputs["requirements-file"], DEV_REQUIREMENTS.name)
        self.assertIn("pytest-xdist", locked_versions())

    def test_the_build_step_finds_its_tools_already_installed(self):
        # Several tests run the build step with the interpreter that runs the suite,
        # and pip installs of one package into one interpreter from parallel workers
        # break each other. With the same versions locked, the step installs nothing.
        steps = load_steps()
        with (
            tempfile.TemporaryDirectory() as runner_temp,
            unittest.mock.patch.dict(os.environ, {"RUNNER_TEMP": runner_temp}),
            unittest.mock.patch.object(steps, "tool") as tool,
            unittest.mock.patch.object(steps, "python", return_value=1),
            self.assertRaises(SystemExit),
        ):
            steps.build()
        installed = dict(call.args[0].split("==") for call in tool.call_args_list)
        self.assertEqual(set(installed), {"build", "twine"})
        locked = locked_versions()
        for name, version in installed.items():
            with self.subTest(tool=name):
                self.assertEqual(locked.get(name), version)


class ToolDefaultTests(unittest.TestCase):
    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_the_pytest_and_uv_defaults_match_the_development_pins(self):
        # Dependabot cannot see input defaults, but it updates requirements-dev.in
        # and its lock, so a new pytest or uv fails here until the default is reviewed.
        inputs = yaml.safe_load(WORKFLOW.read_text())[True]["workflow_call"]["inputs"]
        pinned = locked_versions(ROOT / "requirements-dev.in")
        locked = locked_versions()
        for name in ("pytest", "uv"):
            default = inputs[f"{name}-version"]["default"]
            with self.subTest(tool=name):
                for path, version in (
                    ("requirements-dev.in", pinned.get(name)),
                    ("requirements-dev.txt", locked.get(name)),
                ):
                    self.assertEqual(
                        version,
                        default,
                        f"{path} pins {name} {version}, but the {name}-version "
                        f"default is {default}. CONTRIBUTING says when the default "
                        "may move: for a security fix, or in the next major version. "
                        "Otherwise close the update.",
                    )
        dependabot = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
        ignored = {
            rule["dependency-name"]
            for update in dependabot["updates"]
            for rule in update.get("ignore", [])
        }
        self.assertEqual(ignored & {"pytest", "uv"}, set())


# The opening or closing line of a fenced code block, and its info string.
CODE_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})[ \t]*([^`\s]*)")
CALLED_WORKFLOW = re.compile(r"(?:^|/)\.github/workflows/python-ci\.yml(?:@|$)")


def yaml_blocks(markdown):
    """Yield (line, paragraph, text) for each YAML code block in Markdown.

    line is the number of the opening fence's line, and paragraph the text of the
    run of non-blank lines directly before the fence, or "" after another block. A
    block of any language that never closes runs to the end of the file and hides
    the blocks after it, so it is yielded with text None.
    """
    paragraph, previous_blank = [], True
    fence = None  # (marker, language, line, paragraph, lines) of an open block.
    for number, line in enumerate(markdown.splitlines(), 1):
        marker = CODE_FENCE.match(line)
        if fence is None and marker:
            fence = (marker.group(1), marker.group(2), number, " ".join(paragraph), [])
        elif fence is None:
            if line.strip() and previous_blank:
                paragraph = []
            if line.strip():
                paragraph.append(line.strip())
            previous_blank = not line.strip()
        elif marker and not marker.group(2) and marker.group(1).startswith(fence[0]):
            # A closing fence repeats the opening character at least as often.
            if fence[1].lower() in ("yaml", "yml"):
                yield fence[2], fence[3], "\n".join(fence[4])
            fence, paragraph = None, []
        else:
            fence[4].append(line)
    if fence is not None:
        yield fence[2], fence[3], None


def called_inputs(node):
    """Yield the inputs that parsed YAML passes to python-ci.yml.

    They are the keys of each with: next to a uses: that calls python-ci.yml, or
    next to no uses: at all, as in a fragment that shows only the with: block.
    The with: of a step that uses another action is not a call.
    """
    if isinstance(node, dict):
        uses = node.get("uses")
        calls = uses is None or CALLED_WORKFLOW.search(str(uses))
        if calls and isinstance(node.get("with"), dict):
            yield from map(str, node["with"])
        for value in node.values():
            yield from called_inputs(value)
    elif isinstance(node, list):
        for value in node:
            yield from called_inputs(value)


def snippet_input_problems(name, markdown, declared, unreleased):
    """List the inputs a Markdown file's YAML examples pass that a release lacks.

    GitHub rejects a call that passes an input the called workflow does not declare
    before any job starts, so a caller's pinned release rejects an input that is
    only on main, unless the example says so.
    """
    problems = []
    for line, paragraph, text in yaml_blocks(markdown):
        if text is None:
            problems.append(f"{name} line {line}: the code block has no closing fence")
            continue
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as error:
            problems.append(
                f"{name} line {line}: the YAML block does not parse: {error}"
            )
            continue
        for key in called_inputs(document):
            if key not in declared:
                problems.append(
                    f"{name} line {line}: the example passes {key}, which "
                    "python-ci.yml does not declare"
                )
            elif key in unreleased and "Unreleased" not in paragraph:
                problems.append(
                    f"{name} line {line}: the example passes {key}, whose Added in is "
                    "Unreleased, but the paragraph before it does not say Unreleased"
                )
    return problems


@unittest.skipUnless(yaml, "needs PyYAML")
class DocumentationSnippetTests(unittest.TestCase):
    """Examples are copied into callers that pin a release, not main."""

    DECLARED = frozenset({"python-versions", "released-thing", "new-thing"})
    UNRELEASED = frozenset({"new-thing"})
    CALL = (
        "```yaml\njobs:\n  python:\n    uses: owner/repo/.github/workflows/"
        "python-ci.yml@0123456789abcdef0123456789abcdef01234567 # v1.1.0\n"
        "    with:\n      python-versions: '[\"3.13\"]'\n      {input}: true\n```\n"
    )

    def problems(self, markdown):
        return snippet_input_problems(
            "DOC.md", markdown, self.DECLARED, self.UNRELEASED
        )

    def test_the_readme_and_docs_examples_pass_only_released_inputs(self):
        script = importlib.util.spec_from_file_location(
            "inputs_table", ROOT / ".github/scripts/inputs_table.py"
        )
        assert script and script.loader
        inputs_table = importlib.util.module_from_spec(script)
        script.loader.exec_module(inputs_table)
        cells = inputs_table.expected_cells(
            WORKFLOW.read_text(encoding="utf-8"),
            (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
        )
        unreleased = {name for name, cell in cells.items() if cell[2] == "Unreleased"}
        documents = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
        found = set()
        for path in documents:
            markdown = path.read_text(encoding="utf-8")
            name = path.relative_to(ROOT).as_posix()
            with self.subTest(document=name):
                self.assertEqual(
                    snippet_input_problems(name, markdown, set(cells), unreleased), []
                )
            for _, _, text in yaml_blocks(markdown):
                found.update(called_inputs(yaml.safe_load(text or "")))
        # Full calls and bare with: fragments count; the download step's with: does not.
        self.assertLessEqual({"coverage-fail-under", "matrix-exclude"}, found)
        self.assertIn("upload-dist", found)
        self.assertNotIn("path", found)

    def test_an_undeclared_input_fails(self):
        self.assertEqual(self.problems(self.CALL.format(input="released-thing")), [])
        (problem,) = self.problems(self.CALL.format(input="no-such-input"))
        self.assertEqual(
            problem,
            "DOC.md line 1: the example passes no-such-input, which python-ci.yml "
            "does not declare",
        )

    def test_an_unreleased_input_needs_a_note_in_the_paragraph_before(self):
        call = self.CALL.format(input="new-thing")
        for before, failures in (
            ("Unreleased: `new-thing` is on main only.\n\n", 0),
            ("Run it\nwith `new-thing`, which is Unreleased:\n", 0),
            ("Run it with `new-thing`:\n\n", 1),
            # Only the paragraph directly before the block counts.
            ("`new-thing` is Unreleased.\n\nRun it:\n\n", 1),
            ("Unreleased:\n\n```sh\ntrue\n```\n\n", 1),
        ):
            with self.subTest(before=before):
                problems = self.problems(before + call)
                self.assertEqual(len(problems), failures, problems)
                for problem in problems:
                    self.assertIn(
                        "passes new-thing, whose Added in is Unreleased", problem
                    )

    def test_indented_with_fragments_count_and_other_actions_do_not(self):
        fragment = (
            "Trim the matrix:\n\n```yaml\n    with:\n"
            "      python-versions: '[\"3.13\"]'\n      no-such-input: >-\n"
            '        [{"os": "windows-latest"}]\n```\n'
        )
        (problem,) = self.problems(fragment)
        self.assertEqual(
            problem,
            "DOC.md line 3: the example passes no-such-input, which python-ci.yml "
            "does not declare",
        )
        # A fence indented inside a list item is a block too.
        (problem,) = self.problems(
            "- Trim it:\n\n  ```yml\n  with:\n    new-thing: 1\n  ```\n"
        )
        self.assertIn("passes new-thing, whose Added in is Unreleased", problem)
        other = (
            "```yaml\njobs:\n  python:\n    uses: ./.github/workflows/python-ci.yml\n"
            "  publish:\n    steps:\n      - uses: actions/download-artifact@v8\n"
            "        with:\n          name: dist\n  other:\n"
            "    uses: owner/repo/.github/workflows/python-ci.yml.bak@v1\n"
            "    with:\n      anything: 1\n```\n\n"
            "~~~sh\n```yaml\nwith:\n  no-such-input: 1\n```\n~~~\n"
        )
        self.assertEqual(self.problems(other), [])

    def test_a_yaml_block_that_does_not_parse_or_close_fails(self):
        (problem,) = self.problems("Text.\n\n```yaml\nwith: [\n```\n")
        self.assertTrue(
            problem.startswith("DOC.md line 3: the YAML block does not parse"), problem
        )
        # A block that never closes would hide the blocks after it.
        for language in ("yaml", "sh"):
            with self.subTest(language=language):
                (problem,) = self.problems(
                    f"Text.\n\n```{language}\nwith:\n  no-such-input: 1\n"
                )
                self.assertEqual(
                    problem, "DOC.md line 3: the code block has no closing fence"
                )
