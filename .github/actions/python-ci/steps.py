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
from typing import NamedTuple

VERSION = re.compile(r"|\d+(\.\d+){0,2}")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def flag(name: str, default: bool = False) -> bool:
    return env(name, "true" if default else "false").lower() == "true"


def json_paths(name: str) -> list[str]:
    return json.loads(env(name, '["."]'))


def scratch_dir(prefix: str) -> str:
    """Create a scratch directory under RUNNER_TEMP, which the runner cleans per job.

    Persistent self-hosted runners do not clean the system temp directory, so
    scratch environments and build outputs must not accumulate there.
    """
    base = os.environ.get("RUNNER_TEMP") or None
    if base:
        os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=base)


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


def artifact_suffix() -> str:
    """Return the suffix that keeps artifact names unique per workflow call."""
    if env("CALL_NAME"):
        return "-" + env("CALL_NAME")
    directory = (
        os.path.normpath(env("WORKING_DIRECTORY", ".")).replace("\\", "/").strip("/")
    )
    if directory in ("", "."):
        return ""
    # Artifact names may not contain path separators or a few other characters.
    return "-" + re.sub(r'[/:<>|*?"]+', "-", directory)


def json_list(name: str, default: str) -> list:
    try:
        value = json.loads(env(name) or default)
    except ValueError:
        raise SystemExit(
            f"{name.lower().replace('_', '-')} must be a JSON array"
        ) from None
    if not isinstance(value, list):
        raise SystemExit(f"{name.lower().replace('_', '-')} must be a JSON array")
    return value


def runner_slug(label) -> str:
    """Name a runner label, label array or runner-group object for artifact names."""
    if isinstance(label, dict):
        parts = [label["group"]] if label.get("group") else []
        labels = label.get("labels", [])
        parts += [labels] if isinstance(labels, str) else list(labels)
    elif isinstance(label, list):
        parts = label
    else:
        parts = [label]
    return re.sub(r'[/\\:<>|*?"\s]+', "-", "-".join(str(part) for part in parts))


def matrix_equal(pattern, value) -> bool:
    """Compare two matrix scalars the way GitHub does when it applies an exclude.

    Strings compare without regard to case; any other pair compares as numbers, so
    "3.12" matches 3.12.
    """
    if isinstance(pattern, str) and isinstance(value, str):
        return pattern.casefold() == value.casefold()

    def number(item) -> float:
        if item is None or isinstance(item, (bool, int, float)):
            return float(item or 0)
        try:
            return float(item.strip() or 0)
        except ValueError:
            return float("nan")

    return number(pattern) == number(value)


def matrix_matches(pattern, value) -> bool:
    """Return whether an exclude value matches a matrix value under GitHub's rules.

    An object matches any object that has its keys with matching values, and an
    array matches any array that starts with matching items, so ["self-hosted"]
    also matches ["self-hosted", "linux"].
    """
    if isinstance(pattern, dict):
        if not isinstance(value, dict):
            return False
        keys = {str(key).casefold(): item for key, item in value.items()}
        return all(
            str(key).casefold() in keys
            and matrix_matches(item, keys[str(key).casefold()])
            for key, item in pattern.items()
        )
    if isinstance(pattern, list):
        return (
            isinstance(value, list)
            and len(pattern) <= len(value)
            and all(matrix_matches(p, v) for p, v in zip(pattern, value))
        )
    return not isinstance(value, (dict, list)) and matrix_equal(pattern, value)


def check_matrix_exclude(systems: list, versions: list, primary_version: str) -> None:
    """Validate matrix-exclude against the matrix it trims, matching as GitHub does."""
    excluded = json_list("MATRIX_EXCLUDE", "[]")
    cells = [(system, version) for system in systems for version in versions]
    for entry in excluded:
        if (
            not isinstance(entry, dict)
            or not entry
            or set(entry) - {"os", "python-version"}
        ):
            raise SystemExit(
                f"matrix-exclude entry {json.dumps(entry)} must be an object with only "
                "'os' and/or 'python-version' keys"
            )
        if "os" in entry and not any(matrix_matches(entry["os"], s) for s in systems):
            raise SystemExit(
                f"matrix-exclude entry {json.dumps(entry)} names an os not in operating-systems"
            )
        if "python-version" in entry and not any(
            matrix_matches(entry["python-version"], v) for v in versions
        ):
            raise SystemExit(
                f"matrix-exclude entry {json.dumps(entry)} names a version not in python-versions"
            )
        cells = [
            (system, version)
            for system, version in cells
            if not (
                ("os" not in entry or matrix_matches(entry["os"], system))
                and (
                    "python-version" not in entry
                    or matrix_matches(entry["python-version"], version)
                )
            )
        ]
    if excluded and not cells:
        raise SystemExit("matrix-exclude removes every matrix cell")
    if (
        excluded
        and systems
        and not any(
            system == systems[0] and str(version) == primary_version
            for system, version in cells
        )
    ):
        raise SystemExit(
            "matrix-exclude removes the primary cell (the first operating system with the "
            "first non-experimental Python version), which runs Ruff, pip-audit and the "
            "build. GitHub matches excludes loosely: strings ignore case, and an array or "
            "object also matches runners it is a prefix or subset of."
        )


def matrix_cell() -> dict[str, str]:
    """Describe this job's matrix cell and whether it runs the once-per-run checks.

    The primary cell is the first operating system with the first python version
    that is not experimental, so lint, audit and build failures always count.
    """
    systems = json_list("OPERATING_SYSTEMS", '["ubuntu-latest"]')
    versions = [str(v) for v in json_list("PYTHON_VERSIONS", "[]")]
    experimental = [str(v) for v in json_list("EXPERIMENTAL_PYTHON_VERSIONS", "[]")]
    if any(not isinstance(v, str) for v in json_list("PYTHON_VERSIONS", "[]")):
        print(
            "::warning::python-versions has numbers; quote them, because 3.10 as a "
            "number becomes 3.1"
        )
    for version in experimental:
        if versions and version not in versions:
            print(
                f"::warning::experimental-python-versions lists {version}, which is not "
                "in python-versions"
            )
    for version in versions:
        match = re.search(r"(\d+)\.(\d+)", version)
        if match and (int(match.group(1)), int(match.group(2))) < (3, 11):
            print(
                f"::warning::python-versions lists {version}; the workflow supports "
                "Python 3.11 and later, and its tools may not install on older versions"
            )
    stable = [v for v in versions if v not in experimental]
    if versions and not stable:
        print(
            "::warning::Every entry in python-versions is listed in "
            "experimental-python-versions, so no failure can fail the run"
        )
    primary_version = (stable or versions)[0] if versions else ""
    check_matrix_exclude(systems, json_list("PYTHON_VERSIONS", "[]"), primary_version)
    if not env("MATRIX_PYTHON"):
        # Outside a matrix (for example a local run), this is the only cell.
        return {
            "primary": "true",
            "os-slug": runner_slug(systems[0]) if systems else "",
        }
    current_os = json.loads(env("MATRIX_OS")) if env("MATRIX_OS") else None
    primary = (
        bool(systems)
        and current_os == systems[0]
        and env("MATRIX_PYTHON") == primary_version
    )
    return {
        "primary": "true" if primary else "false",
        "os-slug": runner_slug(current_os),
    }


def write_outputs(values: dict[str, str]) -> None:
    if env("GITHUB_OUTPUT"):
        with open(env("GITHUB_OUTPUT"), "a", encoding="utf-8") as output:
            output.writelines(f"{key}={value}\n" for key, value in values.items())


def warn_about_inputs_without_effect() -> None:
    """Point out input combinations that are accepted but change nothing."""
    if float(env("COVERAGE_FAIL_UNDER") or 0) > 0 and not env("COVERAGE_VERSION"):
        print("::warning::coverage-fail-under has no effect without coverage-version")
    if json.loads(env("AUDIT_IGNORE") or "[]") and not env("AUDIT_VERSION"):
        print("::warning::audit-ignore has no effect without audit-version")
    requirements = env("REQUIREMENTS_FILE")
    if (
        extras()
        and flag("INSTALL_PROJECT")
        and requirements
        and not is_uv_lock(requirements)
    ):
        print(
            "::warning::project-extras with install-project and requirements-file installs "
            "the project with --no-deps, so the extras' dependencies must already be in "
            "requirements-file"
        )


def check_directory(variable: str, label: str) -> None:
    value = env(variable)
    if value and not inside(value).is_dir():
        raise SystemExit(f"{label} must name an existing directory")


def check_version(variable: str, label: str, example: str) -> None:
    if not VERSION.fullmatch(env(variable)):
        raise SystemExit(f"{label} must be empty or a version such as {example}")


def validate() -> int:
    check_directory("TEST_DIRECTORY", "test-directory")
    check_directory("TOP_LEVEL_DIRECTORY", "top-level-directory")
    requirements = env("REQUIREMENTS_FILE")
    if requirements and not inside(requirements).is_file():
        raise SystemExit("requirements-file must name an existing file")
    validate_path_list("COMPILE_PATHS", "compile-paths", "Compilation target")
    validate_path_list("LINT_PATHS", "lint-paths", "Lint target")
    if env("DOCTEST_PATHS", "[]") != "[]":
        validate_path_list("DOCTEST_PATHS", "doctest-paths", "Doctest target")
        validate_doctest_paths()
    check_version("RUFF_VERSION", "ruff-version", "0.6.9")
    check_version("MYPY_VERSION", "mypy-version", "1.11.2")
    check_version("COVERAGE_VERSION", "coverage-version", "7.6.1")
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
    if not VERSION.fullmatch(env("AUDIT_VERSION")):
        raise SystemExit("audit-version must be empty or a version such as 2.10.1")
    try:
        ignore = json.loads(env("AUDIT_IGNORE", "[]"))
    except ValueError:
        ignore = None
    if not isinstance(ignore, list) or any(
        not isinstance(v, str) or not v for v in ignore
    ):
        raise SystemExit("audit-ignore must be a JSON array of vulnerability IDs")
    if env("INSTALLER", "pip") not in ("pip", "uv"):
        raise SystemExit("installer must be pip or uv")
    if not VERSION.fullmatch(env("UV_VERSION")) or (
        env("INSTALLER") == "uv" and not env("UV_VERSION")
    ):
        raise SystemExit("uv-version must be a version such as 0.13.4")
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
    if flag("TEST_WHEEL") and not Path(env("TEST_DIRECTORY") or "tests").is_dir():
        raise SystemExit(
            "test-wheel copies test-directory (tests when empty) into a clean "
            "environment, but that directory does not exist"
        )
    if flag("TEST_WHEEL") and not flag("BUILD_PACKAGE"):
        raise SystemExit("test-wheel requires build-package: true")
    if flag("UPLOAD_DIST") and not flag("BUILD_PACKAGE"):
        raise SystemExit("upload-dist requires build-package: true")
    if flag("BUILD_PACKAGE") and not Path("pyproject.toml").is_file():
        raise SystemExit("build-package requires pyproject.toml in working-directory")
    if env("TEST_VERBOSITY", "2") not in ("0", "1", "2"):
        raise SystemExit("test-verbosity must be 0, 1, or 2")
    if not re.fullmatch(r"[A-Za-z0-9._-]*", env("CALL_NAME")):
        raise SystemExit("call-name may contain only letters, digits, '.', '_' and '-'")
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
    pattern = env("TEST_PATTERN")
    if "/" in pattern or "\\" in pattern:
        raise SystemExit("test-pattern must be a filename glob, not a path")
    re.compile(env("COMPILE_EXCLUDE"))
    warn_about_inputs_without_effect()
    write_outputs({"artifact-suffix": artifact_suffix(), **matrix_cell()})
    return 0


def extras() -> list[str]:
    return [e.strip() for e in env("PROJECT_EXTRAS").split(",") if e.strip()]


def is_uv_lock(path: str) -> bool:
    return Path(path).name == "uv.lock"


def is_pylock(path: str) -> bool:
    return re.fullmatch(r"pylock(\.[^.]+)?\.toml", Path(path).name) is not None


def export_uv_lock(lock: str) -> str:
    """Export a uv.lock, with project-extras, to a hashed requirements file."""
    exported = os.path.join(scratch_dir("uv-export-"), "requirements.txt")
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
        str(Path(lock).parent),
        "-o",
        exported,
    ):
        raise SystemExit(
            "Could not export uv.lock; is it up to date with pyproject.toml?"
        )
    return exported


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
            requirements = export_uv_lock(requirements)
        command = ["-r", requirements]
        hashed = flag("REQUIRE_HASHES", default=True) and not is_pylock(requirements)
        if hashed:
            command.insert(0, "--require-hashes")
        if packages(*command):
            if hashed:
                print(
                    "::error::Installing requirements-file failed with require-hashes: true. "
                    "If the file has no hashes, generate them (pip-compile --generate-hashes or "
                    "uv pip compile --generate-hashes) or set require-hashes: false."
                )
            return 1
    if flag("INSTALL_PROJECT"):
        target = f".[{','.join(extras())}]" if extras() else "."
        # Locked requirements already pin the dependencies; only add the project.
        no_deps = ["--no-deps"] if env("REQUIREMENTS_FILE") else []
        return packages("--editable", target, *no_deps)
    return 0


BOOTSTRAP_PACKAGES = {"pip", "setuptools", "wheel"}


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def audited_requirements() -> list[str]:
    """Pin every installed distribution except editable installs and bootstrap tools.

    pip, setuptools and wheel ship with the interpreter rather than the project, so
    they are audited only when the requirements file pins them.
    """
    import importlib.metadata

    pinned = set()
    if env("REQUIREMENTS_FILE") and Path(env("REQUIREMENTS_FILE")).suffix == ".txt":
        text = Path(env("REQUIREMENTS_FILE")).read_text(encoding="utf-8")
        pinned = {
            canonical(m)
            for m in re.findall(
                r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==", text, re.MULTILINE
            )
        }
    requirements = {}
    for dist in importlib.metadata.distributions():
        name = canonical(dist.metadata["Name"] or "")
        direct = dist.read_text("direct_url.json") or "{}"
        if not name or json.loads(direct).get("dir_info", {}).get("editable"):
            continue
        if name in BOOTSTRAP_PACKAGES and name not in pinned:
            continue
        requirements[name] = f"{name}=={dist.version}"
    return sorted(requirements.values())


def audit() -> int:
    # Run pip-audit from its own environment so it cannot change, or be broken by,
    # the dependencies it is auditing.
    import venv

    tools = scratch_dir("pip-audit-")
    venv.EnvBuilder(with_pip=True).create(tools)
    auditor = os.path.join(tools, "Scripts" if os.name == "nt" else "bin", "python")
    install = [
        auditor,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "-q",
        "pip-audit==" + env("AUDIT_VERSION"),
    ]
    if subprocess.run(install, check=False).returncode:
        raise SystemExit("Could not install pip-audit")
    requirements = audited_requirements()
    if not requirements:
        print("No third-party packages to audit")
        return 0
    listing = os.path.join(tools, "installed.txt")
    Path(listing).write_text("\n".join(requirements) + "\n", encoding="utf-8")
    report = os.path.join(tools, "audit.md")
    command = [
        auditor,
        "-m",
        "pip_audit",
        "-r",
        listing,
        "--no-deps",
        "--disable-pip",
        "--desc",
        "on",
    ]
    command += ["-f", "markdown", "-o", report]
    command += [
        arg
        for vuln in json.loads(env("AUDIT_IGNORE", "[]"))
        for arg in ("--ignore-vuln", vuln)
    ]
    # Advisory text is Unicode; avoid the Windows ANSI code page.
    code = subprocess.run(
        command, check=False, env={**os.environ, "PYTHONUTF8": "1"}
    ).returncode
    text = Path(report).read_text(encoding="utf-8") if os.path.exists(report) else ""
    if text.strip():
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
        print(text)
        if env("GITHUB_STEP_SUMMARY"):
            with open(env("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as summary:
                summary.write(f"### Dependency audit\n\n{text}\n")
    return code


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


DIST_DIRECTORY = "python-ci-dist"


def build() -> int:
    import shutil

    tool("build==1.6.1", "build")
    tool("twine==7.0.0", "twine")
    # A fixed directory under RUNNER_TEMP, so the workflow can upload it afterwards.
    outdir = os.path.join(
        os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), DIST_DIRECTORY
    )
    shutil.rmtree(outdir, ignore_errors=True)
    if python("-m", "build", "--outdir", outdir, "."):
        raise SystemExit("Package build failed")
    if python(
        "-m", "twine", "check", "--strict", *glob.glob(os.path.join(outdir, "*"))
    ):
        return 1
    return test_wheel(outdir) if flag("TEST_WHEEL") else 0


def test_wheel(outdir: str) -> int:
    """Run the test suite against the built wheel in a clean virtual environment."""
    import shutil
    import venv

    wheels = glob.glob(os.path.join(outdir, "*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"Expected one wheel in the build output, found {len(wheels)}")
    workdir = scratch_dir("wheel-test-")
    venv.EnvBuilder(with_pip=True).create(os.path.join(workdir, "venv"))
    interpreter = os.path.join(
        workdir, "venv", "Scripts" if os.name == "nt" else "bin", "python"
    )

    def venv_run(*args: str, cwd: str | None = None) -> int:
        return subprocess.run([interpreter, *args], check=False, cwd=cwd).returncode

    def venv_install(*args: str) -> int:
        return venv_run("-m", "pip", "install", "--disable-pip-version-check", *args)

    # Install the same locked, hash-checked dependency set as the main environment,
    # then add the wheel itself without letting pip resolve anything else.
    requirements = env("REQUIREMENTS_FILE")
    if requirements:
        if is_pylock(requirements):
            locked = python(
                "-m",
                "uv",
                "pip",
                "install",
                "--python",
                interpreter,
                "-r",
                requirements,
            )
        else:
            source = (
                export_uv_lock(requirements)
                if is_uv_lock(requirements)
                else requirements
            )
            hashed = is_uv_lock(requirements) or flag("REQUIRE_HASHES", default=True)
            locked = venv_install(
                *(["--require-hashes"] if hashed else []), "-r", source
            )
        if locked:
            raise SystemExit(
                "Could not install requirements-file into the wheel-test environment"
            )
        if venv_install("--no-deps", wheels[0]):
            raise SystemExit("Could not install the built wheel")
        if venv_run("-m", "pip", "check"):
            raise SystemExit(
                "The wheel's dependencies are not satisfied by requirements-file; "
                "add them to the lock"
            )
    else:
        target = wheels[0] + (f"[{','.join(extras())}]" if extras() else "")
        if venv_install(target):
            raise SystemExit("Could not install the built wheel")
    pytest_runner = env("TEST_RUNNER", "unittest") == "pytest"
    has_pytest = (
        subprocess.run(
            [interpreter, "-c", "import pytest"], check=False, capture_output=True
        ).returncode
        == 0
    )
    if (
        pytest_runner
        and not has_pytest
        and venv_install("-q", "pytest==" + env("PYTEST_VERSION"))
    ):
        raise SystemExit("Could not install pytest for the wheel tests")
    # Copy only the tests and the pytest configuration, so imports resolve to the
    # installed wheel rather than the source tree.
    directory = env("TEST_DIRECTORY") or "tests"
    cwd = os.path.join(workdir, "project")
    shutil.copytree(directory, os.path.join(cwd, directory))
    if pytest_runner:
        for config in (
            "pytest.ini",
            ".pytest.ini",
            "pyproject.toml",
            "tox.ini",
            "setup.cfg",
        ):
            if os.path.isfile(config):
                shutil.copy(config, os.path.join(cwd, config))
    pattern = env("TEST_PATTERN")
    print(f"Testing {os.path.basename(wheels[0])} in a clean environment")
    if pytest_runner:
        files = ["-o", f"python_files={pattern}"] if pattern else []
        return venv_run(
            "-m", "pytest", directory, *files, "-p", "no:cacheprovider", cwd=cwd
        )
    return venv_run(
        "-m",
        "unittest",
        "discover",
        "-s",
        directory,
        "-p",
        pattern or "test*.py",
        cwd=cwd,
    )


def install_test_tools() -> int:
    tools = []
    if env("COVERAGE_VERSION"):
        tools.append("coverage==" + env("COVERAGE_VERSION"))
    if env("TEST_RUNNER", "unittest") == "pytest":
        tools.append("pytest==" + env("PYTEST_VERSION"))
    return pip_install("-q", *tools) if tools else 0


class Record(NamedTuple):
    test_id: str
    outcome: str  # passed, failure, error or skipped
    detail: str
    elapsed: float
    classname: str
    name: str


FIXTURE = re.compile(r"(\w+) \((.+)\)")
# Private, but stable since Python 3.4.
SUBTEST = unittest.case._SubTest  # type: ignore[attr-defined]


def junit_name(test) -> tuple[str, str]:
    """Return the JUnit (classname, name) for a test, subtest or fixture error."""
    if isinstance(test, SUBTEST):
        parent = test.test_case.id()
        classname, _, method = parent.rpartition(".")
        return classname, method + test.id()[len(parent) :]
    match = FIXTURE.fullmatch(test.id())
    if match and "." not in match.group(1):
        # setUpClass, setUpModule and their teardowns: "setUpClass (pkg.mod.Class)".
        return match.group(2), match.group(1)
    classname, _, name = test.id().rpartition(".")
    return classname, name


class RecordingResult(unittest.TextTestResult):
    """Text result that also records each test's outcome and duration."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: list[Record] = []
        self._current = None
        self._started = 0.0

    def startTest(self, test):
        self._current = test
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        self._current = None  # do not keep finished tests alive

    def _record(self, test, outcome, detail=""):
        owner = test.test_case if isinstance(test, SUBTEST) else test
        # Fixture errors such as setUpClass never pass through startTest.
        elapsed = time.perf_counter() - self._started if owner is self._current else 0.0
        self.records.append(
            Record(test.id(), outcome, str(detail), elapsed, *junit_name(test))
        )

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "failure", self.failures[-1][1])

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error", self.errors[-1][1])

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        # Only failing subtests are reported; unittest then never reports the parent.
        if err is not None:
            if issubclass(err[0], test.failureException):
                self._record(subtest, "failure", self.failures[-1][1])
            else:
                self._record(subtest, "error", self.errors[-1][1])

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


SLOWEST = 10


def table_cell(text: str) -> str:
    """Escape text for a Markdown table cell; GitHub splits cells on '|' even in code."""
    return text.replace("|", "\\|").replace("\n", " ")


def build_flavour() -> str:
    import sysconfig

    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        return ""
    gil_enabled = getattr(sys, "_is_gil_enabled", lambda: False)()
    return " free-threaded" + (", GIL re-enabled" if gil_enabled else "")


def write_summary(
    counts: dict[str, int],
    failing: list[tuple[str, str]],
    durations: list[tuple[str, float]],
) -> None:
    summary_path = env("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write(
            f"### Tests on Python {sys.version.split()[0]}{build_flavour()} ({sys.platform})\n\n"
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
        slowest = sorted(durations, key=lambda item: item[1], reverse=True)[:SLOWEST]
        if slowest:
            summary.write(
                f"\n<details><summary>{len(slowest)} slowest tests</summary>\n\n"
                "| Test | Seconds |\n| --- | ---: |\n"
            )
            summary.writelines(
                f"| `{table_cell(test_id)}` | {secs:.2f} |\n"
                for test_id, secs in slowest
            )
            summary.write("\n</details>\n\n")


# XML 1.0 forbids most C0 control characters and lone surrogates.
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")


def xml_text(text: str) -> str:
    return INVALID_XML.sub("\ufffd", text)


def write_junit(path: str, records: list[Record], counts: dict[str, int]) -> None:
    suite = ET.Element(
        "testsuite",
        name="unittest",
        tests=str(len(records)),
        failures=str(counts["failure"]),
        errors=str(counts["error"]),
        skipped=str(counts["skipped"]),
        time=f"{sum(r.elapsed for r in records):.3f}",
    )
    for record in records:
        case = ET.SubElement(
            suite,
            "testcase",
            classname=xml_text(record.classname),
            name=xml_text(record.name),
            time=f"{record.elapsed:.3f}",
        )
        if record.outcome != "passed":
            text = xml_text(record.detail)
            message = text.strip().splitlines()[-1] if text.strip() else record.outcome
            ET.SubElement(case, record.outcome, message=message).text = text
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def doctest_files(path: str) -> list[Path]:
    target = Path(path)
    return sorted(target.rglob("*.py")) if target.is_dir() else [target]


def module_name(file: Path) -> str | None:
    """Return the importable module name of a file under the working directory."""
    resolved = file.resolve()
    root = Path.cwd().resolve()
    if not resolved.is_relative_to(root):
        return None
    parts = list(resolved.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def doctest_modules(paths: list[str]) -> list[tuple[Path, str]]:
    """Translate files and directories under the working directory into modules."""
    return [
        (file, name)
        for path in paths
        for file in doctest_files(path)
        if (name := module_name(file))
    ]


def validate_doctest_paths() -> None:
    """Reject doctest paths that would crash or be skipped instead of running.

    pytest runs any .py file. unittest imports modules by name, so with unittest an
    explicit file must have a module name, and files without one inside a directory
    (for example under .github or a hyphenated directory) are skipped with a warning.
    """
    unittest_runner = env("TEST_RUNNER", "unittest") != "pytest"
    for path in json.loads(env("DOCTEST_PATHS", "[]")):
        target = Path(path)
        if not target.resolve().is_relative_to(Path.cwd().resolve()):
            raise SystemExit(
                f"doctest-paths entry {path!r} is outside working-directory"
            )
        if target.is_file() and target.suffix != ".py":
            raise SystemExit(f"doctest-paths entry {path!r} is not a .py file")
        if not unittest_runner:
            continue
        if target.is_file():
            if module_name(target) is None:
                raise SystemExit(
                    f"doctest-paths entry {path!r} has no module name for unittest to "
                    "import (each directory and file name must be a valid Python "
                    "identifier); use the pytest runner for such files"
                )
            continue
        skipped = [
            file.as_posix() for file in doctest_files(path) if module_name(file) is None
        ]
        if skipped:
            shown = ", ".join(skipped[:5]) + (
                f" and {len(skipped) - 5} more" if len(skipped) > 5 else ""
            )
            print(
                f"::warning::doctest-paths entry {path!r} skips files that have no "
                f"module name for unittest to import: {shown}"
            )


class MalformedDoctest(unittest.TestCase):
    """Report a module whose doctests cannot be parsed as an error in the run."""

    def __init__(self, module: str, message: str) -> None:
        super().__init__("runTest")
        self.module, self.message = module, message

    def id(self) -> str:
        return f"{self.module}.doctests"

    def __str__(self) -> str:
        return f"doctests ({self.module})"

    def runTest(self) -> None:
        raise ValueError(self.message)


def doctest_suite() -> unittest.TestSuite:
    import doctest
    import importlib

    suite = unittest.TestSuite()
    for file, name in doctest_modules(json.loads(env("DOCTEST_PATHS", "[]"))):
        module = importlib.import_module(name)
        try:
            suite.addTest(doctest.DocTestSuite(module))
        except ValueError as error:  # a malformed example
            message = f"Malformed doctest in {file.as_posix()}: {error}"
            suite.addTest(MalformedDoctest(name, message))
    return suite


def no_tests_message(directory: str, pattern: str) -> str:
    return (
        f"No tests discovered (test-directory: {directory!r}, test-pattern: "
        f"{pattern!r}). Point those inputs at your tests, or leave them empty with "
        "the pytest runner to use your pytest configuration."
    )


def run_unittest() -> int:
    # unittest has no project configuration, so empty inputs mean its conventions.
    directory = env("TEST_DIRECTORY") or "tests"
    pattern = env("TEST_PATTERN") or "test*.py"
    suite = unittest.defaultTestLoader.discover(
        directory, pattern=pattern, top_level_dir=env("TOP_LEVEL_DIRECTORY") or None
    )
    suite.addTest(doctest_suite())
    if suite.countTestCases() == 0:
        raise SystemExit(no_tests_message(directory, pattern))
    runner = unittest.TextTestRunner(
        verbosity=int(env("TEST_VERBOSITY", "2")),
        failfast=flag("TEST_FAIL_FAST"),
        resultclass=RecordingResult,  # type: ignore[arg-type]
    )
    result = runner.run(suite)
    assert isinstance(result, RecordingResult)
    records = result.records
    counts = count([r.outcome for r in records])
    write_summary(
        counts,
        [(r.outcome, r.test_id) for r in records if r.outcome in ("failure", "error")],
        [(r.test_id, r.elapsed) for r in records],
    )
    if env("JUNIT_FILE"):
        write_junit(env("JUNIT_FILE"), records, counts)
    return 0 if result.wasSuccessful() else 1


def junit_outcomes(
    path: str,
) -> tuple[dict[str, int], list[tuple[str, str]], list[tuple[str, float]]]:
    outcomes, failing, durations = [], [], []
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
        durations.append((test_id, float(case.get("time") or 0)))
        if outcome in ("failure", "error"):
            failing.append((outcome, test_id))
    return count(outcomes), failing, durations


def run_pytest() -> int:
    import pytest  # type: ignore[import-not-found,unused-ignore]

    report = env("JUNIT_FILE") or os.path.join(scratch_dir("pytest-"), "junit.xml")
    verbosity = {"0": ["-q"], "1": [], "2": ["-v"]}[env("TEST_VERBOSITY", "2")]
    # Empty inputs defer to the project's testpaths and python_files settings.
    args = [
        *([env("TEST_DIRECTORY")] if env("TEST_DIRECTORY") else []),
        *(["-o", f"python_files={env('TEST_PATTERN')}"] if env("TEST_PATTERN") else []),
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
        *verbosity,
        *(["-x"] if flag("TEST_FAIL_FAST") else []),
        *json.loads(env("DOCTEST_PATHS", "[]")),
        *(["--doctest-modules"] if json.loads(env("DOCTEST_PATHS", "[]")) else []),
        *shlex.split(env("PYTEST_ARGS")),
    ]
    code = int(pytest.main(args))
    if code == pytest.ExitCode.NO_TESTS_COLLECTED:
        raise SystemExit(no_tests_message(env("TEST_DIRECTORY"), env("TEST_PATTERN")))
    if os.path.exists(report):
        write_summary(*junit_outcomes(report))
    return 0 if code == pytest.ExitCode.OK else 1


def test() -> int:
    # Import the project under test, not this script's directory.
    sys.path[0] = os.getcwd()
    cov = start_coverage() if env("COVERAGE_VERSION") else None
    code = (
        run_pytest() if env("TEST_RUNNER", "unittest") == "pytest" else run_unittest()
    )
    if cov is not None:
        cov.stop()
        cov.save()
        if cov.get_option("run:parallel"):
            # Merge the data written by subprocesses and pytest-xdist workers.
            cov.combine()
            cov.save()
        data = cov.get_data()
        # Source files that never ran are recorded too, with no lines, so look for
        # executed lines rather than for measured files.
        if not any(data.lines(path) for path in data.measured_files()):
            print(
                "::warning::Coverage collected no data. Check that the tests import the "
                "project, that the coverage omit and include settings match it, and "
                "that no other tool, such as pytest-cov's --cov option, takes over "
                "measurement."
            )
        total = cov.report(skip_empty=True, show_missing=True)
        cov.xml_report(outfile="coverage.xml")
        if env("GITHUB_STEP_SUMMARY"):
            with open(env("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as summary:
                summary.write(
                    f"#### Coverage: {total:.1f}%\n\n<details><summary>Per-file coverage</summary>\n\n"
                )
                cov.report(
                    skip_empty=True,
                    show_missing=True,
                    output_format="markdown",
                    file=summary,
                )
                summary.write("\n</details>\n\n")
        minimum = float(env("COVERAGE_FAIL_UNDER") or 0)
        if total < minimum:
            print(
                f"Coverage {total:.2f}% is below the required {minimum:g}%",
                file=sys.stderr,
            )
            return 1
    return code


def start_coverage():
    """Start coverage.py so that subprocesses and pytest-xdist workers are measured.

    The project's own coverage configuration (omit, branch, exclude_lines and so on)
    is kept; the workflow adds subprocess measurement and parallel data files on top.
    """
    import atexit
    import shutil

    import coverage  # type: ignore[import-not-found,unused-ignore]

    # A fresh data directory: children write their fragments there, so nothing lands
    # in the project and stale fragments from earlier runs are never combined.
    data_dir = scratch_dir("coverage-")
    atexit.register(shutil.rmtree, data_dir, True)
    data_file = os.path.join(data_dir, ".coverage")
    if coverage.version_info < (7, 10, 3):
        print(
            "::notice::coverage.py 7.10.3 or later also measures subprocesses and "
            "pytest-xdist workers"
        )
        cov = coverage.Coverage(data_file=data_file, source=[os.getcwd()])
    else:
        cov = coverage.Coverage(data_file=data_file, config_file=True)
        if cov.config.config_file is None:
            # Subprocess patching needs a configuration file to hand to the children.
            rc = os.path.join(data_dir, "coveragerc")
            Path(rc).write_text("[run]\n", encoding="utf-8")
            cov = coverage.Coverage(data_file=data_file, config_file=rc)
        cov.set_option("run:source", [os.getcwd()])
        cov.set_option("run:patch", [*cov.get_option("run:patch"), "subprocess"])
        cov.set_option("run:parallel", True)
        # The xdist controller collects nothing itself, and a project "include" is
        # overridden by the source above; neither should print in every subprocess.
        cov.set_option(
            "run:disable_warnings",
            [
                *cov.get_option("run:disable_warnings"),
                "no-data-collected",
                "include-ignored",
            ],
        )
    cov.start()
    return cov


STEPS = {
    "validate": validate,
    "install": install,
    "audit": audit,
    "ruff": ruff,
    "mypy": mypy,
    "compile": compile_sources,
    "build": build,
    "install-test-tools": install_test_tools,
    "test": test,
}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in STEPS:
        raise SystemExit(f"usage: steps.py {{{','.join(STEPS)}}}")
    raise SystemExit(STEPS[sys.argv[1]]())
