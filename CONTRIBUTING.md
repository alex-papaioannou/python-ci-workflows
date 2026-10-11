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
python -m unittest discover -s tests
python -m pip install ruff==0.17.0 mypy==2.4.0 actionlint-py==1.7.12.25 zizmor==1.30.1
ruff check . && ruff format --check .
mypy tests .github/actions/python-ci
actionlint
zizmor --offline --min-severity medium .github/workflows
```

Tests find a workflow step by its `- name:`, read which `steps.py` subcommand it
runs, and execute that subcommand in a temporary project. Renaming a step means
updating the tests.

## Releases

Releases follow semantic versioning. A breaking change is removing or renaming an
input or output, changing a default in a way that can fail existing callers, or
dropping a supported Python version. To release, record the changes in
`CHANGELOG.md`, tag `vX.Y.Z`, and move the matching major tag, such as `v1`.
