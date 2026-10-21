# Python CI Workflows

A reusable GitHub Actions workflow for Python projects. By default it checks syntax,
discovers and runs `unittest` or pytest tests, rejects empty test suites, and runs on Python
3.11 through 3.14. It needs no secrets; see [Security posture](#security-posture).
Dependency installation, linting, type checking, coverage and package builds are all
optional.

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

This runs the tests with `unittest`. For pytest tests, set `test-runner: pytest`
under `with:`, or `test-runner: auto` to pick pytest when the project has pytest
configuration, a `conftest.py` in `working-directory` or `test-directory`, or a
`pytest` requirement in one of the places listed under `test-runner` in
[Tests](#tests).

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

- `@v1` follows compatible releases of major version 1. Breaking changes ship only in
  a new major version, and [UPGRADING.md](UPGRADING.md) shows how to move to it. The
  [support policy](#support-policy) says what a release may change.
- `@v1.2.3` or a full commit SHA never changes. A SHA is the most tamper-resistant
  choice, and Dependabot can keep it current.
- `@main` gets unreleased changes and is not recommended for consumers.

`alex-papaioannou/python-ci-workflows` is the permanent path: the repository will not
be renamed or transferred, because every caller's `uses:` line names it and a move
would break those calls.

Checkout runs in the calling repository. Public consumers need this repository to be
public.

## Inputs

All inputs are optional. Paths are relative to `working-directory` and must stay
inside the checkout. JSON arrays are passed as strings.

### Matrix and job

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `python-versions` | string | `["3.11", "3.12", "3.13", "3.14"]` | JSON array of Python versions. Append `t` for a free-threaded (no-GIL) build, such as `3.14t`. Python 3.11 is the oldest supported version; older entries produce a warning. |
| `operating-systems` | string | `["ubuntu-latest"]` | JSON array of runners. Each entry is a label, an array of labels such as `["self-hosted", "linux"]`, or a runner-group object such as `{"group": "large"}`. Every step runs through Python, so Windows and macOS need no shell changes. |
| `matrix-exclude` | string | `[]` | JSON array of cells to skip, each `{"os": ..., "python-version": ...}` with one or both keys, such as `[{"os": "windows-latest", "python-version": "3.12"}]`. Entries match as GitHub matches them: strings ignore case, and an array or object also matches runners it is a prefix or subset of. Validation rejects entries that match nothing and refuses to exclude the primary cell. |
| `allow-prereleases` | boolean | `false` | Let `setup-python` install alpha, beta and release-candidate versions. |
| `experimental-python-versions` | string | `[]` | JSON array of matrix versions whose failures do not fail the run. |
| `max-parallel` | number | `256` | Maximum simultaneous matrix jobs. The default, GitHub's matrix maximum, runs every cell at once; set a lower number to throttle. |
| `timeout-minutes` | number | `10` | Per-job timeout in minutes. GitHub-hosted runners stop jobs after 360 minutes regardless. |
| `cancel-in-progress` | boolean | `false` | Cancel older in-flight jobs for the same workflow, ref and call when a new run starts. |
| `call-name` | string | `""` | Name for this call when one caller workflow calls the workflow more than once with the same `working-directory`. Letters, digits, `.`, `_` and `-`. |
| `result-runner` | string | `ubuntu-latest` | Runner for the final `result` job, which needs no Python: a label, or one runner in JSON as a single `operating-systems` entry would be: an array of labels it must all have, or a runner-group object. Set it when GitHub-hosted runners are unavailable. |
| `working-directory` | string | `.` | Project directory inside the checkout, for monorepos. |

### Dependencies

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `requirements-file` | string | `""` | Locked dependencies installed before checks. With the pip installer, pip's download cache is saved between runs, keyed on this file. A requirements `.txt` works with either installer. With `installer: uv` it may also be a `uv.lock` (exported with `project-extras` and the default groups) or a `pylock.toml`. |
| `install-project` | boolean | `false` | `pip install --editable .` after requirements. With a `requirements-file`, dependencies are assumed locked and installed with `--no-deps`. pip's download cache is keyed on `pyproject.toml` when there is no `requirements-file`. |
| `project-extras` | string | `""` | Comma-separated extras installed with the project, such as `test,dev`. |
| `installer` | string | `pip` | `pip` or `uv`. uv installs into the same interpreter and is much faster on large dependency sets. uv's own cache is not saved between runs. |
| `uv-version` | string | `0.13.4` | uv version used when `installer` is `uv`. |
| `require-hashes` | boolean | `true` | Install with `--require-hashes`. Generate files with `pip-compile --generate-hashes` or `uv pip compile --generate-hashes`. |

### Dependency audit

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `audit-version` | string | `""` | Scan the installed dependencies with this pip-audit version, once per run on the primary cell, against the PyPI vulnerability database. Editable installs and the interpreter's own pip, setuptools and wheel are skipped unless the requirements file pins them. Findings fail the job and appear in the job summary. Empty disables the audit. |
| `audit-ignore` | string | `[]` | JSON array of vulnerability IDs to accept, such as `["GHSA-xxxx-xxxx-xxxx"]`. |

### Tests

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `test-runner` | string | `unittest` | `unittest`, `pytest` or `auto`. `auto` reads the project's files, without importing any of its code, and picks pytest when it finds any of: `pytest.toml`, `.pytest.toml`, `pytest.ini` or `.pytest.ini`; a `[tool.pytest]` or `[tool.pytest.ini_options]` table in `pyproject.toml`; `[pytest]` in `tox.ini`; `[tool:pytest]` in `setup.cfg`; `conftest.py` in `working-directory` or `test-directory`; a requirement named `pytest` in a `.txt` `requirements-file`, `[project.optional-dependencies]` or `[dependency-groups]`; or a non-empty `pytest-args`. Otherwise it picks unittest. The validation step logs the runner; with `auto` it also names the file or setting that decided it and adds that line to the job summary. When unittest discovers no tests but a matching test file defines module-level `test_` functions, the error says to set `test-runner: pytest`; when the project has one of the files or settings above, it says to set `test-runner: pytest` or `auto`. |
| `pytest-version` | string | `9.1.1` | pytest version installed when the test runner is pytest, set directly or picked by `auto`. |
| `pytest-args` | string | `""` | Extra pytest arguments, split like a shell command line, for example `-m "not slow" -n auto`; `-n` needs pytest-xdist in your `requirements-file`. Needs the pytest runner; with `test-runner: auto`, setting it picks pytest. |
| `test-directory` | string | `tests` | Directory to discover tests in. Set it to `""` with the pytest runner to use your pytest `testpaths` instead; unittest treats `""` as `tests`. |
| `test-pattern` | string | `test*.py` | Filename glob for test modules. With pytest it overrides your `python_files` setting, so the default skips `*_test.py` files; set it to `""` to use your pytest configuration and pytest's defaults (`test_*.py` and `*_test.py`). unittest treats `""` as `test*.py`. Discovering zero tests fails the job. |
| `top-level-directory` | string | `""` | unittest import root for package-based layouts. Ignored by pytest, which reads `rootdir` from its own configuration. |
| `test-verbosity` | number | `2` | Output verbosity 0, 1 or 2. For pytest these mean `-q`, the default and `-v`. |
| `test-fail-fast` | boolean | `false` | Stop after the first failing test. Other matrix jobs still finish. |
| `doctest-paths` | string | `[]` | JSON array of modules or package directories whose docstring examples also run as tests, through `doctest` with unittest or `--doctest-modules` with pytest. Paths must be inside `working-directory`. unittest imports each file as a module, so it needs every directory and file name on the way to be a valid Python identifier: it rejects a file entry that is not, and skips such files inside a directory with a warning. pytest runs any `.py` file. |
| `warnings-as-errors` | boolean | `false` | Set `PYTHONWARNINGS=error` for every step. |
| `hash-seed` | string | `0` | `PYTHONHASHSEED`: `random` or an integer from 0 to 4294967295. |
| `junit-file` | string | `""` | Write a JUnit XML report here and upload it as `junit-<os>-<python>`, plus the call suffix described under "Calling the workflow more than once". |

### Syntax, lint and type checks

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `compile-paths` | string | `["."]` | JSON array of files or directories to byte-compile. |
| `compile-exclude` | string | `""` | Python regex of paths to skip while compiling. Match either separator, such as `[/\\]generated[/\\]`. |
| `ruff-version` | string | `""` | Run `ruff check` at this version, once per run on the primary cell. Empty disables Ruff. |
| `ruff-format` | boolean | `true` | Also run `ruff format --check` when Ruff is enabled. |
| `lint-paths` | string | `["."]` | JSON array of paths for Ruff and mypy. |
| `mypy-version` | string | `""` | Type-check `lint-paths` with this mypy version on every matrix job. Empty disables mypy. |

### Coverage and packaging

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `coverage-version` | string | `""` | Measure tests with this coverage.py version. The job summary shows the total and a collapsible per-file table with missing lines, and `coverage.xml` is uploaded as `coverage-<os>-<python>`. Code run in subprocesses and pytest-xdist workers is measured too (coverage.py 7.10.3 or later), and the project's own coverage settings such as `omit` and `branch` apply. |
| `coverage-fail-under` | number | `0` | Fail when total coverage is below this percentage. |
| `test-wheel` | boolean | `false` | After building, install the wheel in a fresh virtual environment and run only a copy of the tests against it. With a `requirements-file` (a `.txt`, `uv.lock` or `pylock.toml`), the locked dependencies are installed first, the same way as for the main tests (same installer, hash-checked unless `require-hashes` is false), then the wheel with `--no-deps`, replacing any copy of the project the lock installed, and the wheel's own requirements must be satisfied by the lock. Without one, the wheel and its `project-extras` resolve normally. pytest is `pytest-version` either way. This catches files missing from the wheel and broken package discovery. Requires `build-package`. |
| `build-package` | boolean | `false` | Build an sdist and wheel and run `twine check --strict`, once per run on the primary cell. Requires `pyproject.toml`. Nothing is published. |
| `upload-dist` | boolean | `false` | After a successful build (and wheel tests, when enabled), upload the files as the `dist` artifact, plus the call suffix, for a publish job in your own workflow. Requires `build-package`. |

### Trimming the matrix

Run every version on Linux and only the oldest and newest elsewhere:

```yaml
    with:
      python-versions: '["3.11", "3.12", "3.13", "3.14"]'
      operating-systems: '["ubuntu-latest", "windows-latest", "macos-latest"]'
      matrix-exclude: >-
        [{"os": "windows-latest", "python-version": "3.12"},
         {"os": "windows-latest", "python-version": "3.13"},
         {"os": "macos-latest", "python-version": "3.12"},
         {"os": "macos-latest", "python-version": "3.13"}]
```

### Publishing to PyPI

PyPI trusted publishing cannot run inside a reusable workflow, so the workflow only
builds and, with `upload-dist`, uploads the `dist` artifact; publish it from a job in
your own workflow.
This suits pure-Python packages; projects with compiled extensions need their own
wheel builds. Because this job can publish, consider pinning the workflow to a full
commit SHA instead of `@v1` (see "Choosing a ref"); zizmor's `unpinned-uses` audit
flags the tag.

```yaml
jobs:
  python:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@v1
    with:
      build-package: true
      test-wheel: true
      upload-dist: true
  publish:
    needs: python
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2
        with:
          name: dist
          path: dist
      - uses: pypa/gh-action-pypi-publish@7245906fb61fa33a13554f9d5fb856fe61758086 # v1.14.3
```

### Calling the workflow more than once

A monorepo can call the workflow once per project from a single caller workflow.
Each call's jobs are scoped by `working-directory`, so calls for different projects
run side by side and `cancel-in-progress` only cancels older runs of the same call.
Give calls that share a `working-directory` a distinct `call-name`:

```yaml
jobs:
  api:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@v1
    with:
      working-directory: services/api
  web:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@v1
    with:
      working-directory: services/web
```

Uploaded artifacts are named after the matrix cell and the call, so calls never
collide: `junit-<os>-<python>` and `coverage-<os>-<python>` gain `-<call-name>`, or
otherwise `-<working-directory>` with `/` replaced by `-`. A single call with the
default `working-directory: .` keeps the plain names.

To serialize or cancel whole caller runs instead, set `concurrency:` on the caller
workflow itself, for example
`concurrency: {group: ci-${{ github.ref }}, cancel-in-progress: true}`.

### The primary cell

Ruff, pip-audit and the package build run once per call, on the primary cell: the
first entry of `operating-systems` with the first entry of `python-versions` that is
not in `experimental-python-versions`. Their failures therefore always fail the run.
The validation step warns when an experimental version is missing from
`python-versions`, when every version is experimental, and when versions are given as
JSON numbers (`3.10` as a number becomes `3.1`).

## Outputs and required checks

A final `result` job waits for the whole matrix and fails unless it succeeded. It runs no Python and checks nothing out, so `result-runner` can point it at any runner. In
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
2. Set up Python, with pip's download cache when `requirements-file` or
   `install-project` is set. uv's own cache is not saved between runs.
3. Validate inputs: paths, JSON arrays, versions, globs and regexes, and choose the
   test runner for `test-runner: auto`. Bad configuration fails here, before any
   project code runs.
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

## Security posture

- Third-party actions are pinned to a full commit SHA, and the step scripts load from
  this repository at the commit of the workflow.
- Jobs request only `permissions: contents: read`, and no job widens it.
- No secrets: the workflow references neither `secrets` nor `github.token`, and
  declares no secrets for a caller to pass.
- Checkout runs with `persist-credentials: false`, so no token is left in the Git
  configuration for project code to read.
- Inputs reach the step scripts only through environment variables: no `run:` script
  contains a `${{ }}` expression. No project code runs before the `Validate inputs`
  step.
- `requirements-file` installs are hash-checked by default (`require-hashes`).

`tests/test_security_posture.py` checks each of these, and weakening one is a
breaking change. [SECURITY.md](SECURITY.md) says what the workflow does not protect
against and how to report a vulnerability.

## Support policy

- **Platforms.** github.com and GitHub Enterprise Cloud are supported. GitHub
  Enterprise Server is not: it lacks the same-repository `$/` action reference that
  loads the step scripts, and `actions/upload-artifact` v4 or later, which uploads
  the reports.
- **Runners.** This repository's CI runs on `ubuntu-latest`, `windows-latest` and
  `macos-latest`. Self-hosted runners need runner version 2.336.0 or newer, `bash` on
  `PATH` (Git Bash on Windows), and `actions/setup-python` support for every version
  in the matrix.
- **Python.** A release's support window is the CPython versions in its default
  `python-versions`: 3.11 through 3.14 in v1. The window changes only when that
  default does, under the rules below, and the planned `python-versions: auto` will
  resolve against it. 3.11 stays in it at least until its end of life in October
  2027. Free-threaded builds such as `3.14t` (tested on Linux), and newer versions
  this repository's CI already tests, such as 3.15, work when listed explicitly.
  Versions older than 3.11 get a warning.
- **Default changes.** An input's default, and what that default does, change only
  in a major release, listed under "Breaking changes" in [CHANGELOG.md](CHANGELOG.md)
  with an entry in [UPGRADING.md](UPGRADING.md) that shows how to keep the old
  behaviour. So a new CPython release joins the default `python-versions`, and an
  end-of-life one leaves it, in the next major release. The one exception is a tool
  version default, such as `uv-version`, whose version has a published
  vulnerability: it moves to a fixed release in a minor or patch release, listed
  under "Security".
- **Pinned tools and actions.** build, twine and the actions the workflow uses, such
  as `actions/checkout`, are pinned without an input. New versions of them may ship
  in a minor release, listed under "Changed defaults", or under "Security" when they
  fix a vulnerability. When an action update needs a newer runner, the self-hosted
  runner floor above rises in the same release, but only to a runner version
  released at least 30 days earlier: GitHub stops queueing jobs on a self-hosted
  runner that has left an update uninstalled for 30 days, so no runner it still
  serves is left out.
- **v2.0.0** carries the default changes scheduled so far: `test-runner`,
  `test-directory`, `test-pattern`, `lint-paths`, coverage that honours the
  project's own `source` and `fail_under` settings, and Python 3.15 in
  `python-versions`.
- **No run-time notice.** A called workflow cannot tell a defaulted input from one
  the caller passed, so it cannot warn the callers that a scheduled default change
  will affect. Such a change is announced in CHANGELOG.md, under "Breaking changes"
  in the Unreleased section as soon as it merges, and in UPGRADING.md, rather than by
  a warning in the run.
- **Hidden failures.** A fix that makes the workflow report a failure it used to
  hide, such as a malformed doctest it skipped, may ship in a minor release under
  "Behaviour changes": a run that passed without really running a check was a
  defect, not a promise. A fix that changes a default or what it does, such as the
  `test-directory`, `test-pattern` and coverage changes above, waits for the next
  major release.
- **Deprecation.** A deprecated input stays declared, and keeps working, until the
  next major release. When a call sets it to anything but its default, the
  "Validate inputs" step prints a `::warning::` that names its replacement.
- **Breaking changes** need a new major version: removing or renaming an input or
  output, changing an input's type or default, weakening the
  [security posture](#security-posture), and any other change that can fail an
  existing caller's run, other than the security fixes, pinned tool and action
  updates and hidden-failure fixes above.
- **Previous major versions.** v1 receives no releases after v2.0.0. From v2 on,
  when a new major version is released, the previous major version receives
  security fixes, as patch releases of its latest release, for six months.

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

This repository's CI runs the same suite with the workflow's pytest runner in four
parallel workers (`pytest-args: "-n 4 --dist worksteal"`, with pytest-xdist from
`requirements-dev.txt`); `python -m pytest -n 4 tests` does the same locally.

The `examples/` directory holds three small consumer projects that the
`Example consumers` workflow runs through the real reusable workflow on every pull
request:

- `stdlib-app`: unittest, doctests, 100% coverage and warnings as errors.
- `src-package`: a src-layout package whose pytest tests run through
  `test-runner: auto`, an editable install with extras, and a package build whose
  wheel is tested on Linux and Windows.
- `uv-project`: dependencies locked in `uv.lock`, installed with uv and audited.

They double as copy-and-paste starting points. This repository's CI also calls the
workflow on itself on Linux, Windows and macOS with most features enabled, and lints the workflow files with actionlint and zizmor. See
[CONTRIBUTING.md](CONTRIBUTING.md) for conventions and [CHANGELOG.md](CHANGELOG.md)
for release history.

## License

MIT. See [LICENSE](LICENSE). The examples carry the same license, so they can be copied
into any project.
