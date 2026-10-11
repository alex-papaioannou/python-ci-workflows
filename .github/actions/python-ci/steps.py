"""Step implementations for the reusable Python CI workflow.

Each step is a subcommand, for example ``python steps.py validate``. Steps read
their configuration from environment variables set by the workflow, so inputs never
reach code through expression interpolation.
"""

from __future__ import annotations

import compileall
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar

VERSION = re.compile(r"|\d+(\.\d+){0,2}")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def flag(name: str, default: bool = False) -> bool:
    return env(name, "true" if default else "false").lower() == "true"


def json_paths(name: str) -> list[str]:
    return json.loads(env(name, '["."]'))


def python(*args: str) -> int:
    return subprocess.run([sys.executable, *args], check=False).returncode


def pip_install(*args: str) -> int:
    return python("-m", "pip", "install", "--disable-pip-version-check", *args)


def tool(requirement: str, label: str) -> None:
    if pip_install("-q", requirement):
        raise SystemExit(f"Could not install {label}")


def inside(value: str) -> Path:
    path = Path(value).resolve()
    root = Path(env("GITHUB_WORKSPACE") or os.getcwd()).resolve()
    if not path.is_relative_to(root):
        raise SystemExit(f"Path is outside the checkout: {value}")
    return path


def validate_path_list(name: str, label: str, missing: str) -> None:
    paths = json_paths(name)
    if (
        not isinstance(paths, list)
        or not paths
        or any(not isinstance(p, str) or not p for p in paths)
    ):
        raise SystemExit(f"{label} must be a nonempty JSON array of path strings")
    for path in paths:
        if not inside(path).exists():
            raise SystemExit(f"{missing} does not exist: {path}")


def validate() -> int:
    for key in ("TEST_DIRECTORY", "TOP_LEVEL_DIRECTORY"):
        value = env(key)
        if value and not inside(value).is_dir():
            raise SystemExit(f"{key} must name an existing directory")
    requirements = env("REQUIREMENTS_FILE")
    if requirements and not inside(requirements).is_file():
        raise SystemExit("requirements-file must name an existing file")
    validate_path_list("COMPILE_PATHS", "compile-paths", "Compilation target")
    validate_path_list("LINT_PATHS", "lint-paths", "Lint target")
    for name, example in (("RUFF", "0.6.9"), ("MYPY", "1.11.2"), ("COVERAGE", "7.6.1")):
        if not VERSION.fullmatch(env(f"{name}_VERSION")):
            raise SystemExit(
                f"{name.lower()}-version must be empty or a version such as {example}"
            )
    try:
        fail_under = float(env("COVERAGE_FAIL_UNDER", "0"))
    except ValueError:
        fail_under = -1
    if not 0 <= fail_under <= 100:
        raise SystemExit("coverage-fail-under must be a number from 0 to 100")
    if env("JUNIT_FILE"):
        inside(env("JUNIT_FILE"))
    if flag("BUILD_PACKAGE") and not Path("pyproject.toml").is_file():
        raise SystemExit("build-package requires pyproject.toml in working-directory")
    if env("TEST_VERBOSITY", "2") not in ("0", "1", "2"):
        raise SystemExit("test-verbosity must be 0, 1, or 2")
    if env("TEST_FAIL_FAST", "false").lower() not in ("true", "false"):
        raise SystemExit("test-fail-fast must be boolean")
    pattern = env("TEST_PATTERN", "test*.py")
    if not pattern or "/" in pattern or "\\" in pattern:
        raise SystemExit("test-pattern must be a nonempty filename glob")
    re.compile(env("COMPILE_EXCLUDE"))
    return 0


def install() -> int:
    command = ["-r", env("REQUIREMENTS_FILE")]
    if flag("REQUIRE_HASHES", default=True):
        command.insert(0, "--require-hashes")
    return pip_install(*command)


def ruff() -> int:
    tool("ruff==" + env("RUFF_VERSION"), "Ruff")
    paths = json_paths("LINT_PATHS")
    failed = python("-m", "ruff", "check", "--output-format=github", *paths)
    if flag("RUFF_FORMAT", default=True):
        failed = python("-m", "ruff", "format", "--check", "--diff", *paths) or failed
    return 1 if failed else 0


def mypy() -> int:
    tool("mypy==" + env("MYPY_VERSION"), "mypy")
    version = "{}.{}".format(*sys.version_info)
    return python("-m", "mypy", "--python-version", version, *json_paths("LINT_PATHS"))


def compile_sources() -> int:
    exclude = re.compile(env("COMPILE_EXCLUDE")) if env("COMPILE_EXCLUDE") else None
    ok = True
    for path in json_paths("COMPILE_PATHS"):
        if not Path(path).exists():
            raise SystemExit(f"Compilation target does not exist: {path}")
        check = (
            compileall.compile_dir if Path(path).is_dir() else compileall.compile_file
        )
        ok = check(path, quiet=1, rx=exclude) and ok
    return 0 if ok else 1


def build() -> int:
    tool("build==1.6.1", "build")
    tool("twine==7.0.0", "twine")
    outdir = tempfile.mkdtemp(prefix="dist-")
    if python("-m", "build", "--outdir", outdir, "."):
        raise SystemExit("Package build failed")
    return python(
        "-m", "twine", "check", "--strict", *glob.glob(os.path.join(outdir, "*"))
    )


def install_coverage() -> int:
    return pip_install("-q", "coverage==" + env("COVERAGE_VERSION"))


class RecordingResult(unittest.TextTestResult):
    """Text result that also records each test's outcome and duration."""

    records: ClassVar[list[tuple[unittest.TestCase, str, str, float]]] = []

    def startTest(self, test):
        self._started = time.perf_counter()
        super().startTest(test)

    def _record(self, test, outcome, detail=""):
        elapsed = time.perf_counter() - getattr(self, "_started", time.perf_counter())
        self.records.append((test, outcome, detail, elapsed))

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "failure", self.failures[-1][1])

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error", self.errors[-1][1])

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "passed")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "failure", "Unexpected success")


def write_summary(records, counts) -> None:
    summary_path = env("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write(
            f"### Tests on Python {sys.version.split()[0]} ({sys.platform})\n\n"
        )
        summary.write(
            "| Passed | Failed | Errors | Skipped |\n| --- | --- | --- | --- |\n"
        )
        summary.write(
            f"| {counts['passed']} | {counts['failure']} | {counts['error']} | {counts['skipped']} |\n\n"
        )
        for test, outcome, _, _ in records:
            if outcome in ("failure", "error"):
                summary.write(f"- {outcome}: `{test.id()}`\n")


def write_junit(path, records, counts) -> None:
    suite = ET.Element(
        "testsuite",
        name="unittest",
        tests=str(len(records)),
        failures=str(counts["failure"]),
        errors=str(counts["error"]),
        skipped=str(counts["skipped"]),
        time=f"{sum(r[3] for r in records):.3f}",
    )
    for test, outcome, detail, elapsed in records:
        classname, _, name = test.id().rpartition(".")
        case = ET.SubElement(
            suite, "testcase", classname=classname, name=name, time=f"{elapsed:.3f}"
        )
        if outcome != "passed":
            text = str(detail)
            message = text.strip().splitlines()[-1] if text.strip() else outcome
            ET.SubElement(case, outcome, message=message).text = text
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def test() -> int:
    # Import the project under test, not this script's directory.
    sys.path[0] = os.getcwd()
    cov = None
    if env("COVERAGE_VERSION"):
        import coverage  # type: ignore[import-not-found,unused-ignore]

        cov = coverage.Coverage(data_file=".coverage", source=[os.getcwd()])
        cov.start()
    suite = unittest.defaultTestLoader.discover(
        env("TEST_DIRECTORY", "tests"),
        pattern=env("TEST_PATTERN", "test*.py"),
        top_level_dir=env("TOP_LEVEL_DIRECTORY") or None,
    )
    if suite.countTestCases() == 0:
        raise SystemExit("No tests discovered")
    runner = unittest.TextTestRunner(
        verbosity=int(env("TEST_VERBOSITY", "2")),
        failfast=flag("TEST_FAIL_FAST"),
        resultclass=RecordingResult,  # type: ignore[arg-type]
    )
    result = runner.run(suite)
    records = RecordingResult.records
    counts = {
        name: sum(1 for r in records if r[1] == name)
        for name in ("passed", "failure", "error", "skipped")
    }
    write_summary(records, counts)
    if env("JUNIT_FILE"):
        write_junit(env("JUNIT_FILE"), records, counts)
    if cov is not None:
        cov.stop()
        cov.save()
        total = cov.report(skip_empty=True)
        cov.xml_report(outfile="coverage.xml")
        minimum = float(env("COVERAGE_FAIL_UNDER") or 0)
        if total < minimum:
            print(
                f"Coverage {total:.2f}% is below the required {minimum:g}%",
                file=sys.stderr,
            )
            return 1
    return 0 if result.wasSuccessful() else 1


def report_result() -> int:
    result = env("MATRIX_RESULT")
    with open(env("GITHUB_OUTPUT"), "a", encoding="utf-8") as output:
        output.write(f"result={result}\n")
    print(f"Matrix result: {result}")
    return 0 if result == "success" else 1


STEPS = {
    "validate": validate,
    "install": install,
    "ruff": ruff,
    "mypy": mypy,
    "compile": compile_sources,
    "build": build,
    "install-coverage": install_coverage,
    "test": test,
    "result": report_result,
}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in STEPS:
        raise SystemExit(f"usage: steps.py {{{','.join(STEPS)}}}")
    raise SystemExit(STEPS[sys.argv[1]]())
