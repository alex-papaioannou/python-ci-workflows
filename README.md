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
Projects requiring pip dependencies need an extension before using this workflow.

The repository's own CI calls the workflow locally and exercises a small runtime
fixture. It does not certify every possible consumer. GitHub-hosted workflow
execution remains necessary to validate permissions and integration.

Suggested next contributions: dependency installation with explicit lockfiles,
optional Ruff checks, and optional package-build validation.

## Workflow behavior tests

The test suite executes the embedded runner in temporary consumer projects. It checks successful root imports, failing tests, import failures, empty discovery, and compilation errors.

Set `python-versions: '["3.11", "3.13"]'` under the reusable job's `with:`. Supply a nonempty JSON array of version strings; GitHub expands it before running jobs.
