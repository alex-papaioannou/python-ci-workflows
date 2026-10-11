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
            process_env.update(env or {})
            return subprocess.run(
                [executable, str(STEPS), script(step)],
                check=False,
                cwd=root,
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
            {"TEST_PATTERN": ""},
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


UV = {"INSTALLER": "uv", "UV_VERSION": "0.11.32"}


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
