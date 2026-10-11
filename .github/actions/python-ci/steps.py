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
import shlex
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
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", e) for e in extras()):
        raise SystemExit("project-extras must be a comma-separated list of extra names")
    if extras() and not (flag("INSTALL_PROJECT") or is_uv_lock(requirements)):
        raise SystemExit(
            "project-extras requires install-project: true or a uv.lock requirements-file"
        )
    if env("INSTALLER", "pip") not in ("pip", "uv"):
        raise SystemExit("installer must be pip or uv")
    if not VERSION.fullmatch(env("UV_VERSION")) or (
        env("INSTALLER") == "uv" and not env("UV_VERSION")
    ):
        raise SystemExit("uv-version must be a version such as 0.11.32")
    if (
        requirements
        and (is_uv_lock(requirements) or is_pylock(requirements))
        and env("INSTALLER", "pip") != "uv"
    ):
        raise SystemExit("uv.lock and pylock.toml requirements need installer: uv")
    if flag("INSTALL_PROJECT") and not (
        Path("pyproject.toml").is_file() or Path("setup.py").is_file()
    ):
        raise SystemExit(
            "install-project requires pyproject.toml or setup.py in working-directory"
        )
    if flag("BUILD_PACKAGE") and not Path("pyproject.toml").is_file():
        raise SystemExit("build-package requires pyproject.toml in working-directory")
    if env("TEST_VERBOSITY", "2") not in ("0", "1", "2"):
        raise SystemExit("test-verbosity must be 0, 1, or 2")
    if env("TEST_RUNNER", "unittest") not in ("unittest", "pytest"):
        raise SystemExit("test-runner must be unittest or pytest")
    if not VERSION.fullmatch(env("PYTEST_VERSION")) or (
        env("TEST_RUNNER") == "pytest" and not env("PYTEST_VERSION")
    ):
        raise SystemExit("pytest-version must be a version such as 9.1.1")
    try:
        shlex.split(env("PYTEST_ARGS"))
    except ValueError as error:
        raise SystemExit(f"pytest-args could not be parsed: {error}") from None
    if env("TEST_RUNNER", "unittest") != "pytest" and env("PYTEST_ARGS"):
        raise SystemExit("pytest-args requires test-runner: pytest")
    if env("TEST_FAIL_FAST", "false").lower() not in ("true", "false"):
        raise SystemExit("test-fail-fast must be boolean")
    pattern = env("TEST_PATTERN", "test*.py")
    if not pattern or "/" in pattern or "\\" in pattern:
        raise SystemExit("test-pattern must be a nonempty filename glob")
    re.compile(env("COMPILE_EXCLUDE"))
    return 0


def extras() -> list[str]:
    return [e.strip() for e in env("PROJECT_EXTRAS").split(",") if e.strip()]


def is_uv_lock(path: str) -> bool:
    return Path(path).name == "uv.lock"


def is_pylock(path: str) -> bool:
    return re.fullmatch(r"pylock(\.[^.]+)?\.toml", Path(path).name) is not None


def install() -> int:
    use_uv = env("INSTALLER", "pip") == "uv"
    if use_uv:
        tool("uv==" + env("UV_VERSION"), "uv")

    def packages(*args: str) -> int:
        if use_uv:
            return python(
                "-m", "uv", "pip", "install", "--python", sys.executable, *args
            )
        return pip_install(*args)

    requirements = env("REQUIREMENTS_FILE")
    if requirements:
        if is_uv_lock(requirements):
            exported = os.path.join(
                tempfile.mkdtemp(prefix="uv-export-"), "requirements.txt"
            )
            extra_args = [arg for name in extras() for arg in ("--extra", name)]
            if python(
                "-m",
                "uv",
                "export",
                "--frozen",
                "--format",
                "requirements-txt",
                "--no-emit-project",
                *extra_args,
                "--directory",
                str(Path(requirements).parent),
                "-o",
                exported,
            ):
                raise SystemExit(
                    "Could not export uv.lock; is it up to date with pyproject.toml?"
                )
            requirements = exported
        command = ["-r", requirements]
        if flag("REQUIRE_HASHES", default=True) and not is_pylock(requirements):
            command.insert(0, "--require-hashes")
        if packages(*command):
            return 1
    if flag("INSTALL_PROJECT"):
        target = f".[{','.join(extras())}]" if extras() else "."
        # Locked requirements already pin the dependencies; only add the project.
        no_deps = ["--no-deps"] if env("REQUIREMENTS_FILE") else []
        return packages("--editable", target, *no_deps)
    return 0


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


def install_test_tools() -> int:
    tools = []
    if env("COVERAGE_VERSION"):
        tools.append("coverage==" + env("COVERAGE_VERSION"))
    if env("TEST_RUNNER", "unittest") == "pytest":
        tools.append("pytest==" + env("PYTEST_VERSION"))
    return pip_install("-q", *tools) if tools else 0


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


def count(outcomes: list[str]) -> dict[str, int]:
    return {
        name: outcomes.count(name) for name in ("passed", "failure", "error", "skipped")
    }


def write_summary(counts: dict[str, int], failing: list[tuple[str, str]]) -> None:
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
        summary.writelines(
            f"- {outcome}: `{test_id}`\n" for outcome, test_id in failing
        )


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


def run_unittest() -> int:
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
    counts = count([r[1] for r in records])
    write_summary(
        counts, [(r[1], r[0].id()) for r in records if r[1] in ("failure", "error")]
    )
    if env("JUNIT_FILE"):
        write_junit(env("JUNIT_FILE"), records, counts)
    return 0 if result.wasSuccessful() else 1


def junit_outcomes(path: str) -> tuple[dict[str, int], list[tuple[str, str]]]:
    outcomes, failing = [], []
    for case in ET.parse(path).getroot().iter("testcase"):
        test_id = f"{case.get('classname')}.{case.get('name')}"
        outcome = next(
            (
                tag
                for tag in ("failure", "error", "skipped")
                if case.find(tag) is not None
            ),
            "passed",
        )
        outcomes.append(outcome)
        if outcome in ("failure", "error"):
            failing.append((outcome, test_id))
    return count(outcomes), failing


def run_pytest() -> int:
    import pytest  # type: ignore[import-not-found,unused-ignore]

    report = env("JUNIT_FILE") or os.path.join(
        tempfile.mkdtemp(prefix="pytest-"), "junit.xml"
    )
    verbosity = {"0": ["-q"], "1": [], "2": ["-v"]}[env("TEST_VERBOSITY", "2")]
    args = [
        env("TEST_DIRECTORY", "tests"),
        "-o",
        f"python_files={env('TEST_PATTERN', 'test*.py')}",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
        *verbosity,
        *(["-x"] if flag("TEST_FAIL_FAST") else []),
        *shlex.split(env("PYTEST_ARGS")),
    ]
    code = int(pytest.main(args))
    if code == pytest.ExitCode.NO_TESTS_COLLECTED:
        raise SystemExit("No tests discovered")
    if os.path.exists(report):
        write_summary(*junit_outcomes(report))
    return 0 if code == pytest.ExitCode.OK else 1


def test() -> int:
    # Import the project under test, not this script's directory.
    sys.path[0] = os.getcwd()
    cov = None
    if env("COVERAGE_VERSION"):
        import coverage  # type: ignore[import-not-found,unused-ignore]

        cov = coverage.Coverage(data_file=".coverage", source=[os.getcwd()])
        cov.start()
    code = (
        run_pytest() if env("TEST_RUNNER", "unittest") == "pytest" else run_unittest()
    )
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
    return code


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
    "install-test-tools": install_test_tools,
    "test": test,
    "result": report_result,
}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in STEPS:
        raise SystemExit(f"usage: steps.py {{{','.join(STEPS)}}}")
    raise SystemExit(STEPS[sys.argv[1]]())
