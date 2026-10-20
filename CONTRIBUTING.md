# Contributing

## Ground rules

- Keep each pull request to one behavior change, with tests and README updates.
- Step logic belongs in `.github/actions/python-ci/steps.py` as a subcommand. Use
  only the standard library and Python 3.9-compatible syntax there, so it runs the
  same way on every supported OS and Python version.
- New inputs are optional and default to today's behavior. Validate them in the
  `Validate inputs` step so bad configuration fails before any project code runs.
- Pin third-party actions to a full commit SHA with a version comment, and pin tool
  versions explicitly.
- Every input needs a `description` and a row in the README inputs table. The test
  suite checks this.

## Local checks

```sh
python -m pip install --require-hashes -r requirements-dev.txt
python -m unittest discover -s tests
python -m pip install ruff==0.17.0 mypy==2.4.0
ruff check . && ruff format --check .
mypy tests .github/actions/python-ci .github/scripts
python -m pip install --require-hashes -r requirements-lint.txt
actionlint
zizmor --strict-collection --min-severity medium .
check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml
```

zizmor also runs its online audits, which check that every pinned SHA matches its
version comment, when `GH_TOKEN` holds a GitHub token; without one it runs the
offline audits only.

Tests find a workflow step by its `- name:`, read which `steps.py` subcommand it
runs, and execute that subcommand in a temporary project. Renaming a step means
updating the tests.

## Releases

Releases follow semantic versioning. A breaking change is removing or renaming an
input or output, changing a default in a way that can fail existing callers, or
dropping a supported Python version.

Releasing is automated. Add a `## [X.Y.Z]` section at the top of `CHANGELOG.md` in
the pull request that should ship. When it merges, the `Release` workflow creates
the `vX.Y.Z` GitHub release from that section and moves the major tag (`vX`) to the
same commit. If the version is already released it does nothing, so editing older
changelog entries is safe.

## Development dependencies

`requirements-dev.in` lists test-only dependencies and `requirements-lint.in` the
workflow linters. Regenerate the hashed lock files after changing them:

```sh
uv pip compile --generate-hashes --universal --python-version 3.11 requirements-dev.in -o requirements-dev.txt
uv pip compile --generate-hashes --universal --python-version 3.13 requirements-lint.in -o requirements-lint.txt
```
