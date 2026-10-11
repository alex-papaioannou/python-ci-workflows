# Python CI Workflows

A reusable GitHub Actions workflow for Python projects. By default it checks syntax,
discovers and runs `unittest` or pytest tests, rejects empty test suites, and runs on Python
3.11 through 3.14. It needs no secrets. Dependency installation, linting, type
checking, coverage and package builds are all optional.

## Quick start

Create `.github/workflows/ci.yml` in the consuming repository:

```yaml
name: CI
on: [push, pull_request]
permissions:
  contents: read
jobs:
  python:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@v1
```

A fuller configuration:

```yaml
jobs:
  python:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@v1
    with:
      python-versions: '["3.11", "3.12", "3.13", "3.14"]'
      operating-systems: '["ubuntu-latest", "windows-latest"]'
      requirements-file: requirements-dev.txt
      ruff-version: "0.17.0"
      mypy-version: "2.4.0"
      coverage-version: "7.16.2"
      coverage-fail-under: 90
      junit-file: test-results/junit.xml
      cancel-in-progress: true
```

### Free-threaded Python

Add a `t` version, such as `3.14t`, to test on the free-threaded build. While
extension modules catch up, it is worth listing it in `experimental-python-versions`
too:

```yaml
    with:
      python-versions: '["3.13", "3.14", "3.14t"]'
      experimental-python-versions: '["3.14t"]'
```

The job summary heading shows whether the GIL was active, so results from the two
builds are easy to tell apart.

### Choosing a ref

- `@v1` follows compatible releases of major version 1.
- `@v1.2.3` or a full commit SHA never changes. A SHA is the most tamper-resistant
  choice, and Dependabot can keep it current.
- `@main` gets unreleased changes and is not recommended for consumers.

Checkout runs in the calling repository. Public consumers need this repository to be
public.

## Inputs

All inputs are optional. Paths are relative to `working-directory` and must stay
inside the checkout. JSON arrays are passed as strings.

### Matrix and job

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `python-versions` | string | `["3.11", "3.12", "3.13", "3.14"]` | JSON array of Python versions. Append `t` for a free-threaded (no-GIL) build, such as `3.14t`. |
| `operating-systems` | string | `["ubuntu-latest"]` | JSON array of runner labels. Every step runs through Python, so Windows and macOS need no shell changes. |
| `allow-prereleases` | boolean | `false` | Let `setup-python` install alpha, beta and release-candidate versions. |
| `experimental-python-versions` | string | `[]` | JSON array of matrix versions whose failures do not fail the run. |
| `max-parallel` | number | `3` | Maximum simultaneous matrix jobs. |
| `timeout-minutes` | number | `10` | Per-job timeout, a whole number from 1 to 360. |
| `cancel-in-progress` | boolean | `false` | Cancel older in-flight jobs for the same workflow and ref when a new run starts. |
| `working-directory` | string | `.` | Project directory inside the checkout, for monorepos. |

### Dependencies

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `requirements-file` | string | `""` | Locked dependencies installed before checks, with pip caching keyed on the file. A requirements `.txt` works with either installer. With `installer: uv` it may also be a `uv.lock` (exported with `project-extras` and the default groups) or a `pylock.toml`. |
| `install-project` | boolean | `false` | `pip install --editable .` after requirements. With a `requirements-file`, dependencies are assumed locked and installed with `--no-deps`. |
| `project-extras` | string | `""` | Comma-separated extras installed with the project, such as `test,dev`. |
| `installer` | string | `pip` | `pip` or `uv`. uv installs into the same interpreter and is much faster on large dependency sets. |
| `uv-version` | string | `0.11.32` | uv version used when `installer` is `uv`. |
| `require-hashes` | boolean | `true` | Install with `--require-hashes`. Generate files with `pip-compile --generate-hashes` or `uv pip compile --generate-hashes`. |

### Dependency audit

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `audit-version` | string | `""` | Scan the installed dependencies with this pip-audit version, once per run, against the PyPI vulnerability database. Editable installs and the interpreter's own pip, setuptools and wheel are skipped unless the requirements file pins them. Findings fail the job and appear in the job summary. Empty disables the audit. |
| `audit-ignore` | string | `[]` | JSON array of vulnerability IDs to accept, such as `["GHSA-xxxx-xxxx-xxxx"]`. |

### Tests

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `test-runner` | string | `unittest` | `unittest` or `pytest`. |
| `pytest-version` | string | `9.1.1` | pytest version installed when `test-runner` is `pytest`. |
| `pytest-args` | string | `""` | Extra pytest arguments, split like a shell command line, for example `-m "not slow" -n auto`. |
| `test-directory` | string | `tests` | Directory to discover tests in. |
| `test-pattern` | string | `test*.py` | Filename glob for test modules (pytest's `python_files`). Discovering zero tests fails the job. |
| `top-level-directory` | string | `""` | unittest import root for package-based layouts. Ignored by pytest, which reads `rootdir` from its own configuration. |
| `test-verbosity` | number | `2` | Output verbosity 0, 1 or 2. For pytest these mean `-q`, the default and `-v`. |
| `test-fail-fast` | boolean | `false` | Stop after the first failing test. Other matrix jobs still finish. |
| `doctest-paths` | string | `[]` | JSON array of modules or package directories whose docstring examples also run as tests, through `doctest` with unittest or `--doctest-modules` with pytest. |
| `warnings-as-errors` | boolean | `false` | Set `PYTHONWARNINGS=error` for every step. |
| `hash-seed` | string | `0` | `PYTHONHASHSEED`: `random` or an integer from 0 to 4294967295. |
| `junit-file` | string | `""` | Write a JUnit XML report here and upload it as `junit-<os>-<python>`. |

### Syntax, lint and type checks

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `compile-paths` | string | `["."]` | JSON array of files or directories to byte-compile. |
| `compile-exclude` | string | `""` | Python regex of paths to skip while compiling. Match either separator, such as `[/\\]generated[/\\]`. |
| `ruff-version` | string | `""` | Run `ruff check` at this version, once per run. Empty disables Ruff. |
| `ruff-format` | boolean | `true` | Also run `ruff format --check` when Ruff is enabled. |
| `lint-paths` | string | `["."]` | JSON array of paths for Ruff and mypy. |
| `mypy-version` | string | `""` | Type-check `lint-paths` with this mypy version on every matrix job. Empty disables mypy. |

### Coverage and packaging

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `coverage-version` | string | `""` | Measure tests with this coverage.py version. The job summary shows the total and a collapsible per-file table with missing lines, and `coverage.xml` is uploaded as `coverage-<os>-<python>`. |
| `coverage-fail-under` | number | `0` | Fail when total coverage is below this percentage. |
| `test-wheel` | boolean | `false` | After building, install the wheel (with `project-extras` and a `.txt` `requirements-file`) in a fresh virtual environment and run only a copy of the tests against it. This catches files missing from the wheel and broken package discovery. Requires `build-package`. |
| `build-package` | boolean | `false` | Build an sdist and wheel and run `twine check --strict`, once per run. Requires `pyproject.toml`. Nothing is published. |

## Outputs and required checks

A final `result` job waits for the whole matrix and fails unless it succeeded. In
branch protection, require the single `<caller job> / result` check instead of
listing every matrix job. The same value is exposed as the `result` output:

```yaml
jobs:
  python:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@v1
  notify:
    needs: python
    if: always()
    runs-on: ubuntu-latest
    steps:
      - run: echo "CI finished with ${{ needs.python.outputs.result }}"
```

Every test job also appends a pass, fail, error and skip table, the IDs of
failing tests, and the ten slowest tests to the job summary.

## How it runs

Each matrix job runs these steps in order. Steps for features you have not enabled
are skipped.

1. Check out the caller's repository without persisting credentials.
2. Set up Python, with pip caching when `requirements-file` is set.
3. Validate inputs: paths, JSON arrays, versions, globs and regexes. Bad
   configuration fails here, before any project code runs.
4. Install requirements and the project, then audit them.
5. Run Ruff, then mypy.
6. Byte-compile `compile-paths`.
7. Build and check the package, and optionally test the built wheel.
8. Run unittest or pytest, optionally under coverage, and write reports.
9. Upload JUnit and coverage artifacts, even when tests fail.

Third-party actions are pinned to full commit SHAs, and tool versions are pinned
explicitly. The step scripts are fetched at the same commit as the workflow, without
checking out anything into your workspace. This needs a GitHub-hosted runner, or a
self-hosted runner on version 2.336.0 or newer.

## Development

Step logic lives in `.github/actions/python-ci/steps.py`, one subcommand per step.
The workflow loads it through the same-repository action reference
`$/.github/actions/python-ci`, so callers always run the scripts from the exact
commit of the workflow they pinned. The tests in `tests/` look up which subcommand
each workflow step runs and execute it against temporary consumer projects:

```sh
python -m pip install --require-hashes -r requirements-dev.txt
python -m unittest discover -s tests
```

This repository's CI calls the workflow locally on Linux, Windows and macOS with
most features enabled, and lints the workflow files with actionlint and zizmor. See
[CONTRIBUTING.md](CONTRIBUTING.md) for conventions and [CHANGELOG.md](CHANGELOG.md)
for release history.
