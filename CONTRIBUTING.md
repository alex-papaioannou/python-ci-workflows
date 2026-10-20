# Contributing

## Ground rules

- Keep each pull request to one behavior change, with tests and README updates.
- Step logic belongs in `.github/actions/python-ci/steps.py` as a subcommand. Use
  only the standard library there, and keep it compatible with Python 3.11, the
  oldest version the workflow supports, so it runs the same way on every supported
  OS and Python version.
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

Every pull request that changes behaviour adds a bullet under `## [Unreleased]` at
the top of `CHANGELOG.md`, in one of these subsections: Added, Changed, Fixed,
Security, Changed defaults, Behaviour changes. Changed defaults and Behaviour
changes list anything that can make an existing caller's run behave differently.

Releasing is automated. To release, open a pull request that renames
`## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` and adds a fresh, empty
`## [Unreleased]` above it. When it merges, the `Release` workflow runs the
smoke test, the example consumers and workflow lint again on that exact commit. Only
if all of them pass does it create the `vX.Y.Z` GitHub release from that section and
move the major tag (`vX`) to the same commit. Releases run only from `main`, in the
`release` environment.

The release job reads the first `## ` section after `## [Unreleased]`. That heading
must be `## [X.Y.Z]` or `## [X.Y.Z] - YYYY-MM-DD` with plain ASCII numbers and no
leading zeros, the section must contain more than empty subsection headings, and the
version must not be lower than the newest existing release; otherwise the job fails.
A heading that looks like a version but is not a level-two heading also fails. Older
sections are not checked.

Only the commit that adds a version to `CHANGELOG.md` publishes it. If that Release
run fails, re-run it: re-runs keep the original commit. A later push never publishes
an older pending version; it fails instead, and running Release manually from `main`
publishes it from the current commit. When the version is already released, the job
changes nothing unless the major tag is missing. It moves an out-of-date major tag
only on a re-run of the releasing commit or a manual run, and otherwise warns, so a
deliberate rollback of `vX` is not undone by the next merge. To roll back, point `vX`
at the previous release and ship the fix as a new patch release.

The `release` environment is configured once in the repository settings: Settings >
Environments > `release` > Deployment branches and tags > Selected branches and tags,
with `main` as the only allowed branch. Until then GitHub creates the environment on
first use without restrictions, and the `github.ref` checks in `release.yml` remain
the only branch guard.

## Development dependencies

`requirements-dev.in` lists test-only dependencies and `requirements-lint.in` the
workflow linters. Regenerate the hashed lock files after changing them:

```sh
uv pip compile --generate-hashes --universal --python-version 3.11 requirements-dev.in -o requirements-dev.txt
uv pip compile --generate-hashes --universal --python-version 3.13 requirements-lint.in -o requirements-lint.txt
```
