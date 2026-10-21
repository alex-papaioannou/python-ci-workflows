# Recipes

Many needs take only a few lines in your own workflow around the call, so the
reusable workflow has no inputs for them. Each recipe below is a complete workflow
for your repository's `.github/workflows` directory: copy it and adapt the names
and paths.

The recipes call the release that the [quick start](../README.md#quick-start) pins,
by full commit SHA, and pass only inputs that release declares. A recipe that relies
on a change that is not in a release yet says Unreleased. The CI recipes trigger
like the quick start: on pushes to `main`, pull requests, merge queues and manual
runs, with a `concurrency` block that cancels a pull request's run when a newer push
supersedes it and never cancels a run on `main`.

- [Publishing to PyPI](#publishing-to-pypi)
- [Uploading coverage to Codecov](#uploading-coverage-to-codecov)
- [Branch protection and merge queues](#branch-protection-and-merge-queues)
- [Monorepos](#monorepos)
- [Testing against several dependency sets](#testing-against-several-dependency-sets)
- [Hashed requirements from Poetry, PDM and pip-tools](#hashed-requirements-from-poetry-pdm-and-pip-tools)
- [Pre-commit hooks](#pre-commit-hooks)
- [pytest timeouts and reruns](#pytest-timeouts-and-reruns)

## Publishing to PyPI

PyPI trusted publishing cannot run inside a reusable workflow, so the workflow
builds the distributions and, with `upload-dist`, uploads them as an artifact for a
job of your own to publish. This release workflow runs when you push a version tag.
The call builds the sdist and wheel once, on the primary cell, tests the built
wheel, and uploads both; the `publish` job then publishes them from the `pypi`
environment, which you register as a trusted publisher on PyPI. It suits
pure-Python packages; projects with compiled extensions need their own wheel
builds.

```yaml
name: Release
on:
  push:
    tags: ["v*"]
permissions:
  contents: read
jobs:
  build:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@49d202ab203e05b7bd8a29376b0fa44188e9bdaa # v1.1.0
    with:
      build-package: true
      test-wheel: true
      upload-dist: true
  publish:
    needs: build
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2
        with:
          # dist plus the call suffix, which is empty for a single call from the
          # repository root that sets no call-name.
          name: dist
          path: dist
      - uses: pypa/gh-action-pypi-publish@7245906fb61fa33a13554f9d5fb856fe61758086 # v1.14.3
```

The artifact is named `dist` plus the call suffix: `-<call-name>` when the call sets
`call-name`, and otherwise `-<working-directory>`, with each `/` or `\` replaced by
`-`. A call that sets `working-directory: packages/core` uploads `dist-packages-core`,
and one that sets `call-name: release` uploads `dist-release`; download that name.
[CONTRIBUTING.md](../CONTRIBUTING.md#public-interface) gives the whole rule.

Release builds should not restore a cache. The build installs build, twine and your
`[build-system] requires` without hash checks, and a run for a tag push can restore
a cache saved by any workflow run on the default branch, including one that handles
untrusted input. Unreleased: from the next release, setup-python restores and saves
no pip cache in a call with `upload-dist`, on any matrix cell. The release this page
pins still uses pip's cache in such a call when `requirements-file` or
`install-project` is set, so this call sets neither. A src-layout project, whose
tests import the installed package, needs `install-project: true`, which turns that
cache back on, so do not publish one with this recipe until you pin a release after
v1.1.0.

Keeping the release build in a workflow of its own leaves your CI calls their pip
cache. Pin this workflow to a full commit SHA, as here, rather than `@v1`, because
it publishes what the reusable workflow built.

## Uploading coverage to Codecov

With `coverage-version` set, every matrix cell uploads its `coverage.xml` as the
artifact `coverage-<os>-<python>`, plus the call suffix. This workflow downloads
them all and sends them to Codecov in one upload, authenticated with OpenID Connect
instead of a stored token. Only the `codecov` job can request an OIDC token.

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
  # Needed only when a merge queue protects the branch.
  merge_group:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}
  cancel-in-progress: true
jobs:
  python:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@49d202ab203e05b7bd8a29376b0fa44188e9bdaa # v1.1.0
    with:
      coverage-version: "7.16.2"
  codecov:
    needs: python
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      # Codecov reads the checkout to match the report's paths to your files.
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2
        with:
          # Each artifact holds a file named coverage.xml, so each one goes into a
          # directory named after it instead of overwriting the others.
          pattern: coverage-*
          merge-multiple: false
          path: coverage-reports
      - uses: codecov/codecov-action@303a32d7a59b442fa8d48b6a1cc6825c09c847a5 # v7.1.1
        with:
          use_oidc: true
          directory: coverage-reports
          fail_ci_if_error: true
```

Add the repository on Codecov first; with OIDC it needs no token secret. A pull
request from a fork cannot request an OIDC token, so for one the action uploads
without a token, which Codecov accepts for public repositories. The upload runs
only when the whole call succeeded. In a workflow that calls the workflow more than
once, `coverage-*` matches the reports of every call; narrow it with the call
suffix, such as `coverage-*-services-api`.

## Branch protection and merge queues

The workflow ends with a `result` job that fails unless every matrix cell
succeeded, so branch protection or a ruleset needs one required check per call,
whatever the matrix: `python / result` for a job named `python` that calls the
workflow. A merge queue runs only the workflows that trigger on `merge_group`, and
waits for its required checks from those runs, so keep that trigger in every
workflow that reports a required check.

This workflow also tests pull requests on the oldest and newest Python only, and
`main` and the merge queue on every version. `python-versions` takes an expression,
and the check is still `python / result`, so the smaller matrix needs no other
setting. With a merge queue, a change passes the full matrix before it merges;
without one, the run on `main` after the merge is the first to test every version.

To combine the call with jobs of your own, such as a documentation build, add a job
that uses [alls-green](https://github.com/re-actors/alls-green) and require only
its check, here `all-green`, instead of `python / result`. A job that `needs` others
is skipped when one of them fails, and a skipped required check counts as passed,
so the `all-green` job runs `if: always()` and fails when any needed job failed or
was cancelled. A job that is skipped by design, such as one that runs only when its
files change, goes in `allowed-skips`; any other skipped job fails the check.

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
  merge_group:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}
  cancel-in-progress: true
jobs:
  python:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@49d202ab203e05b7bd8a29376b0fa44188e9bdaa # v1.1.0
    with:
      # The oldest and newest Python on pull requests, every version otherwise.
      python-versions: ${{ github.event_name == 'pull_request' && '["3.11", "3.14"]' || '["3.11", "3.12", "3.13", "3.14"]' }}
  changes:
    # Lists the files a pull request changes. Other runs build everything.
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    permissions:
      pull-requests: read
    outputs:
      docs: ${{ steps.filter.outputs.docs }}
    steps:
      - uses: dorny/paths-filter@ceb8a2b8f2d89434be7ff52d3de7ec3738c5cc9d # v4.0.3
        id: filter
        with:
          filters: |
            docs:
              - "docs/**"
  docs:
    needs: changes
    # Skipped only on a pull request that leaves docs/ alone.
    if: ${{ !cancelled() && !failure() && needs.changes.outputs.docs != 'false' }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.13"
      - run: python -m pip install --require-hashes -r docs/requirements.txt
      - run: python -m sphinx --fail-on-warning docs docs/_build/html
  all-green:
    if: always()
    needs: [python, changes, docs]
    runs-on: ubuntu-latest
    steps:
      - uses: re-actors/alls-green@b5b5b37504aa4183270bd3d855c52a67f212be35 # v1.3.0
        with:
          # changes runs only on pull requests, and docs only when docs/ changed.
          allowed-skips: changes, docs
          jobs: ${{ toJSON(needs) }}
```

Do not filter the whole workflow with `paths:` under `pull_request`: a pull request
that touches no matching file then never reports the required check, which stays
pending and blocks the merge.

## Monorepos

A monorepo calls the workflow once per project, with each project's
`working-directory`; paths in the other inputs, such as `requirements-file`, are
relative to it. Each call needs a required check of its own, here `api / result`,
`web / result` and `api-integration / result`, or an
[alls-green job](#branch-protection-and-merge-queues) that needs all of them. Two
calls for the same `working-directory` need distinct `call-name` values.

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
  # Needed only when a merge queue protects the branch.
  merge_group:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}
  cancel-in-progress: true
jobs:
  api:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@49d202ab203e05b7bd8a29376b0fa44188e9bdaa # v1.1.0
    with:
      working-directory: services/api
      requirements-file: requirements.txt
      junit-file: test-results/junit.xml
  api-integration:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@49d202ab203e05b7bd8a29376b0fa44188e9bdaa # v1.1.0
    with:
      working-directory: services/api
      # A second call for the same project needs a name of its own.
      call-name: api-integration
      requirements-file: requirements.txt
      test-directory: integration_tests
      python-versions: '["3.13"]'
      junit-file: test-results/junit.xml
  web:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@49d202ab203e05b7bd8a29376b0fa44188e9bdaa # v1.1.0
    with:
      working-directory: services/web
      python-versions: '["3.13", "3.14"]'
```

The calls run side by side, and each uploads its reports under its own suffix:
`junit-ubuntu-latest-3.13-services-api` from `api`, and
`junit-ubuntu-latest-3.13-api-integration` from `api-integration`. Ruff, the audit and
the package build run once per call, on that call's primary cell.

## Testing against several dependency sets

A job that calls the workflow can have a `strategy.matrix` of its own, and each
entry is a separate call. This one tests the lowest versions your project allows on
the oldest Python and the newest versions on every Python, each from its own locked
file. Every entry needs its own `call-name`, so that the calls' reports and
concurrency groups stay apart, and the job `name` keeps the required checks short:
`python (lowest) / result` and `python (latest) / result`.

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
  # Needed only when a merge queue protects the branch.
  merge_group:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}
  cancel-in-progress: true
jobs:
  python:
    name: python (${{ matrix.dependencies }})
    strategy:
      # Finish the other set when one fails.
      fail-fast: false
      matrix:
        include:
          - dependencies: lowest
            requirements: requirements/lowest.txt
            python-versions: '["3.11"]'
          - dependencies: latest
            requirements: requirements/latest.txt
            python-versions: '["3.11", "3.12", "3.13", "3.14"]'
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@49d202ab203e05b7bd8a29376b0fa44188e9bdaa # v1.1.0
    with:
      call-name: ${{ matrix.dependencies }}
      requirements-file: ${{ matrix.requirements }}
      python-versions: ${{ matrix.python-versions }}
      install-project: true
```

uv 0.13.4 writes both files, with hashes and for every platform, from the
dependencies in `pyproject.toml` and its `test` extra. `lowest-direct` picks the
lowest version each of your own requirements allows:

```sh
uv pip compile pyproject.toml --extra test --universal --generate-hashes --resolution lowest-direct -o requirements/lowest.txt
uv pip compile pyproject.toml --extra test --universal --generate-hashes -o requirements/latest.txt
```

With `test-runner: pytest`, the workflow installs `pytest-version` after the
requirements file, replacing the locked pytest; set `pytest-version` in each entry
to test the locked one.

## Hashed requirements from Poetry, PDM and pip-tools

`require-hashes` is on by default, so pip installs `requirements-file` only when it
pins every package, including the dependencies of dependencies, to one version with
its hashes. Each tool below writes such a file from its own lock. The commands were
run once against [`examples/src-package`](../examples/src-package), whose `test`
extra needs pytest, and the file installed with `--require-hashes` and passed its
tests. Commit the file and point the workflow at it; with `install-project`, the
workflow then installs your project without its dependencies, which the file
already holds.

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
  # Needed only when a merge queue protects the branch.
  merge_group:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}
  cancel-in-progress: true
jobs:
  python:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@49d202ab203e05b7bd8a29376b0fa44188e9bdaa # v1.1.0
    with:
      requirements-file: requirements.txt
      install-project: true
      test-runner: pytest
```

### Poetry

Verified with Poetry 2.5.1 and poetry-plugin-export 1.10.1. `poetry export` comes
from the plugin, which Poetry 2 no longer installs; add it with
`poetry self add poetry-plugin-export`. Use `--with <group>` for a Poetry dependency
group instead of an extra.

```sh
poetry lock
poetry export --extras test --output requirements.txt
```

### PDM

Verified with PDM 2.29.3. `pdm lock` leaves out optional-dependency groups such as
`test` unless you name them with `--group`, and `pdm export` includes hashes unless
told otherwise.

```sh
pdm lock --group test
pdm export --group test --output requirements.txt
```

### pip-tools

Verified with pip-tools 7.6.2. pip-compile resolves for the Python version and
platform it runs on, so the file can miss a package that only another platform
needs: pytest needs colorama on Windows, which a file compiled on Linux leaves out,
and a Windows runner then refuses to install the file. Run it on each platform you
test, or use `uv pip compile --universal --generate-hashes` as under
[Testing against several dependency sets](#testing-against-several-dependency-sets).

```sh
pip-compile --generate-hashes --extra test --output-file requirements.txt pyproject.toml
```

A uv project needs no export: point `requirements-file` at `uv.lock`. Unreleased:
the release this page pins also needs `installer: uv` for it, as the
[`uv-project` example's caller](../examples/uv-project/.github/workflows/ci.yml)
sets; from the next release, a `uv.lock` installs with uv by default.

## Pre-commit hooks

Hooks need no Python matrix, so run them in a job of their own next to the call.
This one runs the hooks in `.pre-commit-config.yaml` (or `prek.toml`) on every file
with [prek](https://github.com/j178/prek), a fast reimplementation of pre-commit,
through its action. Require its check, `hooks`, as well as `python / result`, or
combine both with an [alls-green job](#branch-protection-and-merge-queues). If a hook
runs Ruff, leave `ruff-version` unset so Ruff does not run twice.

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
  # Needed only when a merge queue protects the branch.
  merge_group:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}
  cancel-in-progress: true
jobs:
  python:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@49d202ab203e05b7bd8a29376b0fa44188e9bdaa # v1.1.0
  hooks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: j178/prek-action@44924fe9ef4a6f88253c4b6b1bf11c689d3ce53d # v3.1.0
        with:
          # The newest prek whose checksum this version of the action carries, so
          # prek changes only when the action does.
          prek-version: latest-known
```

[pre-commit.ci](https://pre-commit.ci) is the alternative that needs no workflow: a
GitHub App that runs the same configuration on every pull request, reports a check
of its own and can push fixes to the branch.

## pytest timeouts and reruns

pytest plugins are configured through `pytest-args` and installed from
`requirements-file`. This workflow fails any test that runs longer than five
minutes, with a traceback of where it hung, instead of waiting for the job's
`timeout-minutes`, and reruns a failed test up to twice before failing it. That
holds on Linux and macOS. Windows has no `SIGALRM`, so there pytest-timeout ends the
whole test run when a test hangs: the test is not rerun, the tests after it do not
run, and that cell writes no JUnit report, job summary or coverage report.

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
  # Needed only when a merge queue protects the branch.
  merge_group:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}
  cancel-in-progress: true
jobs:
  python:
    uses: alex-papaioannou/python-ci-workflows/.github/workflows/python-ci.yml@49d202ab203e05b7bd8a29376b0fa44188e9bdaa # v1.1.0
    with:
      test-runner: pytest
      requirements-file: requirements-test.txt
      pytest-args: --timeout=300 --reruns=2 --reruns-delay=5
```

Lock `requirements-test.txt` with hashes from a `requirements-test.in` that pins
pytest at the release's `pytest-version`, so the workflow's own pytest install
finds it in place. uv 0.13.4 locks it for every platform and Python 3.11 and later:

```text
pytest==9.1.1
pytest-timeout==2.4.0
pytest-rerunfailures==16.7
```

```sh
uv pip compile requirements-test.in --universal --generate-hashes --python-version 3.11 -o requirements-test.txt
```

Reruns hide flaky tests. A test that fails and then passes on a rerun counts as
passed: the JUnit report and the job summary list each of its attempts as a pass,
and only the `RERUN` lines in the log show the failure. Use reruns while you fix
the flaky tests, not in place of fixing them, or rerun only tests marked with
`@pytest.mark.flaky(reruns=2)` and leave `--reruns` out.
