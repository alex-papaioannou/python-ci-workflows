# Changelog

This project follows [semantic versioning](https://semver.org/). Consumers can pin a
major tag such as `v1`, an exact release, or a commit SHA.

## [Unreleased]

### Breaking changes

### Added

- UPGRADING.md, the guide to moving to a new major version. Each breaking change gets
  an entry there that shows the calling workflow before and after.
- A support policy in the README: the supported platforms, the self-hosted runner
  floor, the Python support window, when input defaults change and how the changes
  are announced, how deprecated inputs warn, which changes may ship in a minor
  release, and how long a previous major version receives security fixes.
  CONTRIBUTING's definition of a breaking change and SECURITY.md's supported
  versions match it.
- A security posture section in the README, with the same guarantees as design rules
  in CONTRIBUTING: actions pinned to commit SHAs, only `contents: read`, no secrets
  or `github.token`, checkout without persisted credentials, no `${{ }}` expression
  in a `run:` script, inputs validated before any project code runs, and
  hash-checked requirements by default. The test suite checks each one, and
  weakening one is a breaking change. Both documents also state that
  `alex-papaioannou/python-ci-workflows` is the permanent path and will not be
  renamed or transferred.
- `test-runner: auto`, which picks pytest when the project has a pytest
  configuration file or table, a `conftest.py`, a `pytest` requirement in a `.txt`
  `requirements-file`, `[project.optional-dependencies]` or `[dependency-groups]`,
  or a non-empty `pytest-args`, and unittest otherwise. It reads files only and
  imports nothing. The validation step logs the chosen runner and the file or
  setting that decided it, and adds them to the job summary. With an explicit
  runner it logs only the runner. The default stays `unittest`.
- When unittest discovers no tests but a matching test file defines module-level
  `test_` functions, or the project has pytest configuration, the error now says to
  set `test-runner: pytest`. It also suggests `auto` when the project has one of the
  files or settings that make `auto` pick pytest.
- The README's examples pin the newest release by full commit SHA with a
  `# vX.Y.Z` comment, which zizmor's `unpinned-uses` audit accepts, and the quick
  start shows `@v1` as the convenience option. A new "Staying up to date" section
  gives a consumer `dependabot.yml` that groups minor and patch updates and sets a
  cooldown, and explains how to watch releases. The test suite checks that the
  pinned SHA is the commit of the release its comment names.
- Release notes end with the release commit's full SHA, a `uses:` line pinned to it
  and a link comparing the release with the previous one.
- Each matrix cell's job summary ends with a table of its checks: validation,
  installation, the audit, Ruff, mypy, the syntax check, the build, the test tools
  and the tests. It gives the outcome of each and the tool versions requested, such
  as `mypy 2.4.0`. The outcome is success, failure, skipped, or not enabled when its
  input leaves it off; on the other cells the audit, Ruff and the build read
  "skipped (runs on the primary cell)". The table is written whenever validation
  ran, even after a failure.
- A public interface section in CONTRIBUTING lists the names callers depend on: the
  workflow's path, the inputs with their types and defaults, the outputs, the
  `result` job's id and name, which the required `<caller job> / result` check
  matches, the artifact names `dist`, `junit-<os>-<python>` and
  `coverage-<os>-<python>` with the call suffix, and the primary-cell rule. Changing
  any of them is a breaking change; log text, the job summary layout, step names and
  the matrix test job names may change in any release. The README says the same to
  callers and lists the outputs in a table. The test suite pins the `result` job's
  id and name and each artifact name, and checks that the README documents every
  output.
- An "Added in" column in the README inputs tables, naming the release that
  introduced each input. `python .github/scripts/inputs_table.py --write` generates
  it, with the Type and Default columns, from `python-ci.yml` and the Added
  subsections of CHANGELOG.md. The test suite fails when the README differs, or when
  a new input has no Added bullet that starts with "`<name>` input".
  `requirements-dev.in` now pins uv next to pytest, and a test fails when either pin
  differs from the `uv-version` or `pytest-version` default, so a Dependabot update
  of either tool fails CI until the default is reviewed.

### Changed

- A `uv.lock` or `pylock.toml` `requirements-file` now installs with uv when
  `installer` is left at its default, where validation used to fail. Only uv reads
  these files, so `installer` has no effect with them, even when set to `pip`. The
  validation step logs the installer and the reason, such as
  `Installer: uv (requirements-file is uv.lock)`, adds that line to the job summary
  when `requirements-file` or `install-project` is set, and passes the resolved
  installer to the install and build steps. The default stays `pip`.
- The release job fails when a section has Breaking changes entries but keeps the
  major version, or raises the major version without any. It also releases no new
  version while Unreleased has Breaking changes entries, so breaking changes can no
  longer ship in a 1.x release. Each Breaking changes entry links its UPGRADING.md
  entry, which the test suite checks, and the release notes point those links at
  the released tag.
- This repository's smoke test runs its own test suite in four parallel
  pytest-xdist workers through the workflow's pytest runner. `requirements-dev.txt`
  now also locks build and twine at the versions the build step installs, so
  parallel tests never install them into the same interpreter at once.
- Release notes list Breaking changes, Changed defaults and Behaviour changes before
  the other subsections.

### Fixed

- The README said the `src-package` example's wheel is tested on Linux and Windows,
  but the build and the wheel test run only on the primary cell. The example list,
  the `test-wheel` row, "The primary cell" and "How it runs" now say so.

### Security

- A call with `upload-dist` no longer has setup-python restore or save a pip cache,
  on any of its matrix cells. A tag-push run could restore a cache saved by any
  workflow on the default branch, and the build installs build and twine by version
  and the build backend from the project's `[build-system] requires`, all without
  hashes, so a poisoned cache entry could reach the published distributions. To keep
  the cache on runs that do not publish, set `upload-dist` only on the runs that do,
  or call the workflow a second time without it and give that call a `call-name`.
  The workflow declares no `cache-mode`, so a `cache-mode` on the calling job limits
  it without stopping it from starting. The README's security posture and
  CONTRIBUTING's design rules list both guarantees, and the test suite checks them.

### Changed defaults

### Behaviour changes

- The tests now run after lint, type, compile and build failures, that is when Ruff,
  mypy, the syntax check or the package build fails, and after a failed dependency
  audit. They depend only on validation and on installing the dependencies and test
  tools. One mypy or syntax error used to skip the tests on every matrix cell, and a
  Ruff or build failure skipped them on the primary cell. The job still fails, and
  `dist` is still uploaded only when every earlier step succeeded. The syntax check
  and the build still need every earlier step to succeed.

## [1.1.0] - 2026-10-21

### Added

- An MIT license. The example projects declare it too, so they can be copied freely.
- `call-name` input to tell apart several calls from one caller workflow.
- `matrix-exclude` input to skip matrix cells, validated against the matrix with
  GitHub's matching rules.
- `upload-dist` input to upload the built distributions as the `dist` artifact after
  a successful build, so a job in the calling workflow can publish them; the README
  shows a trusted-publishing recipe.
- `result-runner` input to choose the runner of the final `result` job, which no
  longer needs Python or the step scripts: a label, a JSON array of labels, or a
  runner-group object.
- With the pytest runner, an empty `test-directory` or `test-pattern` now means "use
  the project's pytest configuration": the workflow passes neither a path nor
  `python_files`, so `testpaths`, `python_files` and pytest's `*_test.py` default
  apply. The defaults are unchanged for now; they still override that configuration.
- Warnings for experimental versions missing from `python-versions`, an
  all-experimental matrix, Python versions given as JSON numbers, and Python
  versions older than 3.11, the oldest supported version.
- Warnings for inputs that have no effect: `coverage-fail-under` without
  `coverage-version`, `audit-ignore` without `audit-version`, and `project-extras`
  whose dependencies a `requirements-file` must already provide.
- A failed hash-checked install now explains `require-hashes` and how to generate
  hashes.

### Changed defaults

- `max-parallel` defaults to 256, so the whole matrix starts at once; it used to be
  3, which split even the default four-version matrix into two waves. Set
  `max-parallel` explicitly to keep throttling.

### Fixed

- Several calls of the workflow from one caller workflow no longer share matrix
  concurrency groups. Calls for different `working-directory` values used to run one
  after another, could cancel each other's pending jobs, and with
  `cancel-in-progress: true` could cancel each other's running jobs.
- Uploaded JUnit and coverage artifacts no longer collide when one caller workflow
  calls the workflow more than once: their names gain a per-call suffix.
- Ruff, pip-audit and the package build run on the first non-experimental Python
  version. They used to run on the first listed version, so with a newest-first list
  whose first entry was experimental their failures could not fail the run.
- `operating-systems` entries may be label arrays or runner-group objects. Those
  runners used to skip Ruff, pip-audit and the build without a warning.
- "No tests discovered" now names `test-directory` and `test-pattern` and their
  values.
- With unittest, failing and erroring subtests are now counted in the job summary
  and written to the JUnit report; they used to be missing from both.
- `setUpClass` and `setUpModule` errors are reported in JUnit under the class or
  module that failed, with a zero duration.
- JUnit reports stay valid XML when failure messages contain control characters,
  such as ANSI colour codes, and test IDs containing `|` no longer break the
  summary tables.
- Coverage now includes code run in subprocesses and pytest-xdist workers
  (coverage.py 7.10.3 or later). Under `pytest -n` it used to report 0%, failing
  any threshold and crashing with `warnings-as-errors`.
- `doctest-paths` entries outside `working-directory` or naming a file other than
  `.py` are rejected by validation instead of being skipped or crashing the test
  step. With unittest, directory entries warn about the files they skip because
  their paths are not module names, such as files under `.github` or a hyphenated
  directory.
- Scratch environments and build output (pip-audit, wheel tests, `dist`, uv
  exports, JUnit fallbacks, coverage data) are created under `RUNNER_TEMP`, which
  the runner cleans after each job, instead of accumulating in the system temp
  directory of persistent self-hosted runners.

### Behaviour changes

- `test-wheel` installs the locked dependencies from `requirements-file` (a `.txt`,
  `uv.lock` or `pylock.toml`) the same way as the main environment, then the wheel
  with `--no-deps`. It used to resolve the wheel's dependencies afresh, so it could
  test against versions the lock does not allow, and it ignored `uv.lock` and
  `pylock.toml`. It now fails when the wheel's own requirements are missing from the
  lock.
- With unittest, a malformed doctest under `doctest-paths` now fails the run as a
  test error naming the file; it used to be skipped silently.
- With unittest, a `doctest-paths` file entry whose path is not a module name, such
  as one under a hyphenated directory, now fails validation; it used to be skipped
  silently. The pytest runner accepts such files.
- Artifact names for calls with a `working-directory` other than `.`, or with a
  `call-name`, end in `-<call-name>` or `-<working-directory>`. Single calls from
  the repository root keep their names.

### Changed

- Releases are published only from `main`, and only after the smoke test, the
  example consumers and workflow lint pass again on the released commit.
- The release job rejects malformed, misplaced, empty and out-of-order changelog
  versions instead of falling back to the previous release, tells API errors apart
  from missing tags, publishes a version only from the commit that added it, and
  leaves a manually rolled-back major tag alone on ordinary pushes.

### Security

- The default `uv-version` is now 0.13.4. Earlier uv releases trust a root
  certificate authority with compromised intermediate keys (GHSA-hq85-5cpj-mr62),
  which allows interception of package downloads. Callers that pin `uv-version`
  should move to 0.13.4 or later.
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
- Early validation of inputs.

### Testing

- Example consumer projects exercised end to end on every pull request.

### Security

- Releases and the moving major tag are published by a workflow from the changelog.
- Third-party actions pinned to commit SHAs.
- Workflow files linted with actionlint and zizmor.
