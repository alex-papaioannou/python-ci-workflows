# Python CI Workflows

Reusable GitHub Actions CI for Python projects that use only the standard library.
Checks syntax, discovers `unittest` tests under `tests/`, rejects empty test suites,
and runs on Python 3.11, 3.12, and 3.13. No package installation or secrets required.

## Usage

Create `.github/workflows/ci.yml` in a consuming repository:

```yaml
name: CI
on: [push, pull_request]
permissions:
  contents: read
jobs:
  python:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@main
```

Use a reviewed commit SHA instead of `main` for stable consumers. Public consumers
require this repository to be public. Checkout runs in the calling repository.
Projects requiring pip dependencies can set `requirements-file`.

The repository's own CI calls the workflow locally and exercises a small runtime
fixture. It does not certify every possible consumer. GitHub-hosted workflow
execution remains necessary to validate permissions and integration.

Suggested next contributions:
optional Ruff checks, and optional package-build validation.

## Workflow behavior tests

The test suite executes the embedded runner in temporary consumer projects. It checks successful root imports, failing tests, import failures, empty discovery, and compilation errors.

Set `python-versions: '["3.11", "3.13"]'` under the reusable job's `with:`. Supply a nonempty JSON array of version strings; GitHub expands it before running jobs.

`test-directory` defaults to `tests`. Paths are relative to the project working directory.

`test-pattern` defaults to `test*.py`. A custom pattern that discovers zero tests still fails the job.

`working-directory` defaults to `.`. Use a repository-relative directory for monorepo projects; compilation, installation, and tests run there.

Set `top-level-directory` for package-based discovery. Discovered directories must be importable according to unittest's package rules; the default leaves discovery to infer its root.

`test-verbosity` accepts 0, 1, or 2; the default remains verbose output (2).

`test-fail-fast: true` stops unittest after its first failure. Matrix fail-fast remains disabled so other Python versions finish.

`timeout-minutes` defaults to 10. Use a positive whole number within GitHub's hosted runner limit (360).

`max-parallel` defaults to 3. Set a positive whole number to bound concurrent matrix jobs; this does not cancel older workflow runs.

`compile-paths` is a JSON array, default `["."]`. Missing targets fail; select source paths to avoid generated or vendored code.

`compile-exclude` is an optional Python regular expression matched against paths by compileall; it does not affect test discovery.

An early validation step rejects nonexistent/out-of-checkout paths, invalid JSON target lists, verbosity values, filename globs, and exclusion regexes. GitHub validates matrix and job-level settings before steps can run.

Set `warnings-as-errors: true` to set `PYTHONWARNINGS=error` for job steps. This can reveal deprecations in dependencies as well as project code.

`hash-seed` defaults to `"0"` for reproducible ordering. Use `"random"` to exercise randomized hashing, or a decimal integer from 0 through 4294967295. Python rejects invalid seeds before executing code.

`requirements-file` installs a pip requirements file (relative to `working-directory`) before syntax checks and tests, with pip caching keyed on that file. `require-hashes` defaults to `true`, so every requirement must be pinned with `--hash`; generate such a file with `pip-compile --generate-hashes` or `uv pip compile --generate-hashes`. Set it to `false` only for trusted, unhashed requirement files.

Set `ruff-version` (for example `"0.17.0"`) to run `ruff check` and `ruff format --check` on the first Python version in the matrix. Findings appear as pull request annotations. Set `ruff-format: false` to lint without enforcing formatting. `lint-paths` is a JSON array, default `["."]`; Ruff reads the project's own `pyproject.toml` or `ruff.toml`.

Set `mypy-version` (for example `"2.4.0"`) to type-check `lint-paths` with mypy on every Python version in the matrix. mypy reads `[tool.mypy]` from the project's `pyproject.toml`; install third-party stubs through `requirements-file`.

Set `coverage-version` (for example `"7.16.2"`) to measure the test run with coverage.py. The job prints a report, uploads `coverage.xml` as the `coverage-<python version>` artifact, and fails when the total falls below `coverage-fail-under` (default 0). Configure omissions in `[tool.coverage]`.

Set `build-package: true` to build an sdist and wheel with `python -m build` and validate their metadata with `twine check --strict`, once per run on the first matrix Python version. It requires `pyproject.toml` in `working-directory`. Nothing is published.
