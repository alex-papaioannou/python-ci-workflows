"""Run the workflow's step scripts against temporary consumer projects."""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/python-ci.yml"
STEPS = ROOT / ".github/actions/python-ci/steps.py"


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


class StepHarness:
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
                path.write_text(content)
            process_env = os.environ.copy()
            process_env["GITHUB_WORKSPACE"] = str(root)
            process_env["GITHUB_STEP_SUMMARY"] = os.devnull
            process_env["GITHUB_OUTPUT"] = os.devnull
            process_env.update(env or {})
            return subprocess.run(
                [executable, str(STEPS), script(step)],
                check=False,
                cwd=root / working_directory,
                env=process_env,
                text=True,
                capture_output=True,
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
            (
                {"WORKING_DIRECTORY": "services/api", "CALL_NAME": "api-lint"},
                "-api-lint",
            ),
        )
        for env, suffix in cases:
            with self.subTest(env=env):
                values, _ = self.validate_outputs(env)
                self.assertEqual(values["artifact-suffix"], suffix)

    def test_uploaded_artifact_names_use_the_suffix(self):
        names = re.findall(
            r"^          name: (.*)$", WORKFLOW.read_text(), re.MULTILINE
        )
        self.assertTrue(names)
        for name in names:
            self.assertTrue(
                name.endswith("${{ steps.validate.outputs.artifact-suffix }}"), name
            )

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
        files = {
            "pyproject.toml": '[tool.coverage.run]\nomit = ["*"]\n',
            "tests/test_ok.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
        }
        result = self.execute_keep(files, {"COVERAGE_VERSION": "7.16.2"})
        self.assertIn("::warning::Coverage measured no files", result.stdout)

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
            process_env = {
                **os.environ,
                "GITHUB_WORKSPACE": str(root),
                "GITHUB_STEP_SUMMARY": os.devnull,
                "GITHUB_OUTPUT": os.devnull,
                **env,
            }
            result = subprocess.run(
                [
                    sys.executable,
                    str(STEPS),
                    script("Run tests and reject empty test suites"),
                ],
                check=False,
                cwd=root,
                env=process_env,
                text=True,
                capture_output=True,
            )
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

    def test_result_job_reports_matrix_outcome(self):
        for outcome, code in (("success", 0), ("failure", 1), ("cancelled", 1)):
            with tempfile.TemporaryDirectory() as out:
                output = Path(out) / "output"
                result = self.execute(
                    {},
                    {"MATRIX_RESULT": outcome, "GITHUB_OUTPUT": str(output)},
                    step="Report matrix result",
                )
                self.assertEqual(result.returncode, code, outcome)
                self.assertEqual(output.read_text(), f"result={outcome}\n")

    def test_every_input_is_described_and_documented(self):
        readme = (WORKFLOW.parents[2] / "README.md").read_text()
        documented = set(re.findall(r"^\| `([a-z-]+)` \|", readme, re.MULTILINE))
        lines = WORKFLOW.read_text().splitlines()
        start = lines.index("    inputs:") + 1
        declared = []
        for index, line in enumerate(lines[start:], start):
            if not line.startswith("      "):
                break
            if re.fullmatch(r"      [a-z-]+:", line):
                declared.append(line.strip()[:-1])
                self.assertTrue(
                    lines[index + 1].strip().startswith("description: "), line
                )
        self.assertEqual(set(declared), documented)

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
            ({"INSTALLER": "conda"}, {}),
            ({"INSTALLER": "uv", "UV_VERSION": ""}, {}),
            ({"REQUIREMENTS_FILE": "uv.lock"}, {"uv.lock": ""}),
            ({"REQUIREMENTS_FILE": "pylock.toml"}, {"pylock.toml": ""}),
        )
        for env, files in cases:
            result = self.execute(
                {"tests/test_ok.py": "", **files}, env, step="Validate inputs"
            )
            self.assertNotEqual(result.returncode, 0, env)
        ok = self.execute(
            {"tests/test_ok.py": "", "uv.lock": ""},
            {**UV, "REQUIREMENTS_FILE": "uv.lock", "PROJECT_EXTRAS": "test"},
            step="Validate inputs",
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)


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

    def test_wheel_requires_build(self):
        result = self.execute(
            {"tests/test_ok.py": ""}, {"TEST_WHEEL": "true"}, step="Validate inputs"
        )
        self.assertNotEqual(result.returncode, 0)


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


def load_steps():
    spec = importlib.util.spec_from_file_location("python_ci_steps", STEPS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
