# Changelog

This project follows [semantic versioning](https://semver.org/). Consumers can pin a
major tag such as `v1`, an exact release, or a commit SHA.

## [1.0.0]

First tagged release.

### Added

- Optional dependency vulnerability scanning with pip-audit (`audit-version`, `audit-ignore`).
- uv as an alternative installer, with `uv.lock` and `pylock.toml` support (`installer`, `uv-version`).
- Editable installation of the project itself with optional extras (`install-project`, `project-extras`).
- pytest as an alternative test runner (`test-runner`, `pytest-version`, `pytest-args`).
- Python 3.14 in the default version matrix.
- Pip requirements installation, hash-checked by default, with caching.
- Optional Ruff lint and format checks.
- Optional mypy type checking.
- Optional coverage measurement with a minimum threshold and an uploaded report.
- Optional sdist and wheel builds checked with `twine check --strict`.
- Optional cancellation of superseded runs.
- Job summary of test results and optional JUnit XML reports.
- Operating system matrix covering Linux, Windows and macOS.
- Pre-release and experimental Python versions that do not block the run.
- A single aggregated `result` check and workflow output.
- Configurable Python versions, test discovery, verbosity, fail-fast, timeout,
  parallelism, compile targets and exclusions, warnings-as-errors, and hash seed.
- Early validation of all inputs.

### Security

- Third-party actions pinned to commit SHAs, with Dependabot updates.
- Workflow files linted with actionlint and zizmor.
