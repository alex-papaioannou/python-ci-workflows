# Changelog

This project follows [semantic versioning](https://semver.org/). Consumers can pin a
major tag such as `v1`, an exact release, or a commit SHA.

## [Unreleased]

### Added

- `call-name` input to tell apart several calls from one caller workflow.

### Fixed

- Several calls of the workflow from one caller workflow no longer share matrix
  concurrency groups. Calls for different `working-directory` values used to run one
  after another, could cancel each other's pending jobs, and with
  `cancel-in-progress: true` could cancel each other's running jobs.

### Security

- `actions/checkout` 7.0.1, `actions/setup-python` 7.0.0 and
  `actions/upload-artifact` 7.0.2, still pinned to full commit SHAs.
- SECURITY.md now states which installs are hash-checked and which are pinned by
  version only, and that the major tag moves on every release.
- Dependabot updates for the pinned actions and the development requirements now
  run; the previous configuration was invalid.
- The workflow files, the composite action and the Dependabot configuration are
  linted strictly, including zizmor's online audits of the pinned SHAs.

## [1.0.0]

First tagged release.

### Added

- Free-threaded Python versions such as `3.14t`, labelled in the job summary.
- Doctest collection (`doctest-paths`) and the ten slowest tests in the job summary.
- Testing the built wheel in a clean environment (`test-wheel`).
- Optional dependency vulnerability scanning with pip-audit (`audit-version`, `audit-ignore`).
- uv as an alternative installer, with `uv.lock` and `pylock.toml` support (`installer`, `uv-version`).
- Editable installation of the project itself with optional extras (`install-project`, `project-extras`).
- pytest as an alternative test runner (`test-runner`, `pytest-version`, `pytest-args`).
- Python 3.14 in the default version matrix.
- Pip requirements installation, hash-checked by default, with caching.
- Optional Ruff lint and format checks.
- Optional mypy type checking.
- Optional coverage measurement with a minimum threshold, a per-file job summary table and an uploaded report.
- Optional sdist and wheel builds checked with `twine check --strict`.
- Optional cancellation of superseded runs.
- Job summary of test results and optional JUnit XML reports.
- Operating system matrix covering Linux, Windows and macOS.
- Pre-release and experimental Python versions that do not block the run.
- A single aggregated `result` check and workflow output.
- Configurable Python versions, test discovery, verbosity, fail-fast, timeout,
  parallelism, compile targets and exclusions, warnings-as-errors, and hash seed.
- Early validation of all inputs.

### Testing

- Example consumer projects exercised end to end on every pull request.

### Security

- Releases and the moving major tag are published by a workflow from the changelog.
- Third-party actions pinned to commit SHAs.
- Workflow files linted with actionlint and zizmor.
