"""Execute the actual embedded workflow runner against temporary consumers."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/python-ci.yml"


def script(name):
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("      - name: " + name)
    start = lines.index("        run: |", start) + 1
    block = []
    for line in lines[start:]:
        if line and not line.startswith("          "):
            break
        block.append(line[10:])
    return "\n".join(block)


class WorkflowTests(unittest.TestCase):
    def execute(self, files, env=None, step="Run tests and reject empty test suites"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            process_env = os.environ.copy()
            process_env["GITHUB_WORKSPACE"] = str(root)
            process_env.update(env or {})
            return subprocess.run(
                [sys.executable, "-c", script(step)],
                check=False,
                cwd=root,
                env=process_env,
                text=True,
                capture_output=True,
            )

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
        for line in WORKFLOW.read_text().splitlines():
            if "uses:" in line:
                self.assertRegex(line, r"@[0-9a-f]{40}\b", line)

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
