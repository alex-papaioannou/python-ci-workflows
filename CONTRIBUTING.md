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
- Every input and output needs a `description` and a row in the README's inputs or
  outputs table. A new input's bullet in the Added subsection of `CHANGELOG.md`
  starts with `` `<name>` input ``, as in
  `` `call-name` input to tell apart several calls ``.
  `python .github/scripts/inputs_table.py --write` fills in the row's Type, Default
  and "Added in" columns from `python-ci.yml` and `CHANGELOG.md`. The test suite
  checks all of this.

## Design rules

These rules are the workflow's [security posture](README.md#security-posture), and
`tests/test_security_posture.py` checks them. Weakening one is a breaking change,
even when no caller's run would fail.

- Pin third-party actions in `python-ci.yml` and `action.yml` to a full commit SHA,
  and load the step scripts only through the same-repository `$/` reference. The
  only `./` reference is the caller's own pre-test hook.
- `python-ci.yml` requests `permissions: contents: read` at the top level, and no job
  widens it.
- Neither `python-ci.yml` nor `action.yml` references `secrets` or `github.token`,
  and `python-ci.yml` declares no secrets.
- Every checkout sets `persist-credentials: false`.
- No `run:` script contains a `${{ }}` expression. Inputs reach `steps.py` through
  `env:` blocks, and no step before `Validate inputs` runs code from the caller's
  repository.
- `require-hashes` defaults to `true`.
- setup-python's `cache` input is empty in a call with `upload-dist`, and
  `python-ci.yml` declares no `cache-mode` at any level.

`alex-papaioannou/python-ci-workflows` is the permanent path: the repository will not
be renamed or transferred, because every caller's `uses:` line names it.

## Public interface

Callers depend on these names and rules in their own workflows, branch protection
and rulesets, so changing or removing one is a breaking change; adding one is not.

- The workflow's path, `.github/workflows/python-ci.yml` in this repository, which
  every caller's `uses:` line names.
- The inputs, each with its type and default and what that default does, as the
  README's inputs tables list them.
- The outputs, which the README's outputs table lists: `result`.
- The `result` job's id and its `name:`, both `result`, on a job with no matrix.
  Branch protection and rulesets match required checks by name, and the README tells
  callers to require `<caller job> / result`. Under a new name, or with a matrix that
  adds its values to the name, that required check stays pending and blocks every
  merge.
- The artifact names `dist`, `junit-<os>-<python>` and `coverage-<os>-<python>`, each
  plus the call suffix. Publish jobs and report tools download the artifacts by name.
  - The call suffix is `-<call-name>`, or otherwise `-<working-directory>`: the
    normalized path without leading or trailing separators, with each run of `/`,
    `\`, `:`, `<`, `>`, `|`, `*`, `?` and `"` replaced by `-` and spaces kept. It is
    empty when `call-name` is empty and `working-directory` is `.`, the defaults.
  - `<os>` is the runner label, or for a label array or runner-group object its group
    and labels joined with `-`, with each run of those characters or of whitespace
    replaced by `-`.
- The pre-test hook: every cell runs `.github/actions/python-ci-pre-test/action.yml`
  from the caller's repository, when it exists, after `Validate inputs` and before
  installing dependencies, with the inputs `python-version`, `os`,
  `working-directory` and `call-name`.
- The primary-cell rule: Ruff, pip-audit, the package build and the wheel test run
  once per call, on the first entry of `operating-systems` with the first entry of
  `python-versions` that is not in `experimental-python-versions`, or the first entry
  of `python-versions` when every entry is experimental.

Not part of the interface, and free to change in any release: log text and layout,
the job summary layout, step names, and the names of the matrix test jobs.

`tests/test_workflow_contract.py` pins the `result` job's id, its name and its lack
of a matrix, and each upload step's artifact name. It also checks that the README
documents every input and output. A change that adds an output or an artifact adds it
to this list and to those tests.

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

CI runs the same suite through the workflow's pytest runner in four pytest-xdist
workers; `python -m pytest -n 4 tests` runs it the same way locally. Tests must
therefore pass in any order and in separate processes. Some steps pip-install a tool
into the interpreter running the tests, and parallel installs of one package into
one interpreter break each other, so `requirements-dev.in` pins build and twine at
the versions the build step installs; a test checks they agree. Change them in
`steps.py` and the lock together; Dependabot leaves them alone. The smoke job's
`Audit dependencies` step audits them and their dependencies too, so an advisory
against any of them fails the smoke test, and blocks releases, until the lock moves.

`requirements-dev.in` also pins pytest and uv at the `pytest-version` and
`uv-version` defaults, and a test checks that they agree. Dependabot cannot see the
defaults in `python-ci.yml`, but it updates these pins, so its pull request for a new
pytest or uv fails until someone reviews the default. Move the default with the pin
only as the [support policy](README.md#support-policy) allows: under Security when
the new version fixes a vulnerability in the default, or under Breaking changes for
the next major version. Otherwise close the pull request.

## Releases

Releases follow semantic versioning and the README's
[support policy](README.md#support-policy). A breaking change, which needs a new
major version, is removing or renaming an input or output; changing an input's type,
its default or what that default does; changing or removing anything else listed
under [Public interface](#public-interface), such as the `result` check's name or an
artifact name; weakening one of the design rules above; or any other change that can
fail an existing caller's run. Adding a Python version to the default
`python-versions`, or removing one, changes a default. A minor release may still
ship three kinds of change that can fail a run: a security fix that moves a
tool version default, listed under Security, which may also ship in a patch release;
a new version of something the workflow pins without an input (build, twine and the
pinned actions), listed under Changed defaults, which may raise the self-hosted
runner floor as the support policy describes; and a fix that makes the workflow
report a failure it used to hide, listed under Behaviour changes, unless the fix
changes a default or what it does.

A deprecated input stays declared until the next major version. When a call sets it
to anything but its default, the `Validate inputs` step prints a `::warning::` that
names its replacement; the first deprecation adds that check to `steps.py`.

Every pull request that changes behaviour adds a bullet under `## [Unreleased]` at
the top of `CHANGELOG.md`, in one of these subsections: Breaking changes, Added,
Changed, Fixed, Security, Changed defaults, Behaviour changes. The Unreleased
section keeps each of them as a heading, in that order, even while it is empty, and
the test suite checks that it does. A breaking change as defined above goes under
Breaking changes as soon as it merges, even when it changes a default. Any entry
there makes the next release a new major version, and each one needs an entry in
[UPGRADING.md](UPGRADING.md) that shows the calling workflow before and after. That
entry is a `###` heading under the section for the new major version, such as
`## From v1 to v2`, and the bullet links to it, as in
`[UPGRADING.md](UPGRADING.md#input-x-is-removed)`. The release notes point these
links at UPGRADING.md in the released tag, so they also work on the release page.
Changed defaults and Behaviour changes list the other changes that can make an
existing caller's run behave differently.

Releasing is automated. To release, open a pull request that renames
`## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, deletes the subsections that stayed
empty, and adds a fresh `## [Unreleased]` with every subsection heading above it.
In the same pull request, run `python .github/scripts/inputs_table.py --write`,
which replaces Unreleased in the README's "Added in" column with the new version.
The released section becomes the release notes, so the test suite fails on one with
an empty subsection. The notes list Breaking changes, Changed defaults and Behaviour
changes first, and end with the release commit's full SHA, a `uses:` line pinned to
it and, from the second release on, a link comparing the release with the previous
one. When the pull request merges, the `Release` workflow runs the smoke test, the
example consumers and workflow lint again on that exact commit. Only if all of them
pass does it create the `vX.Y.Z` GitHub release from that section and move the major
tag (`vX`) to the same commit. Releases run only from `main`, in the `release`
environment.

After each release, open a pull request that updates the pinned SHA in the README:
replace every `uses:` line pinned to the previous release, `# vX.Y.Z` comment
included, with the line from the new release's notes. The release commit cannot
name its own SHA, and `release.yml` does not commit to `main`, which changes only
through pull requests. The test suite fails while the quick start pins a release
older than the two newest in `CHANGELOG.md`, so the next release pull request cannot
merge before this one. It also fails when the pinned SHA is not the commit of the
release the comment names, which it looks up on GitHub when the tag is not fetched.

From v2 on, the support policy gives the previous major version security fixes for
six months, as patch releases from a maintenance branch. Before v3.0.0 ships, three
things that tie releases to `main` must change: the push trigger and `github.ref`
checks in `release.yml`, the `release` environment's allowed branches, and the
release job's check that a version is not lower than the newest release, which on a
maintenance branch must compare only with releases of the same major version.

The release job reads the first `## ` section after `## [Unreleased]`. That heading
must be `## [X.Y.Z]` or `## [X.Y.Z] - YYYY-MM-DD` with plain ASCII numbers and no
leading zeros, the section must contain more than empty subsection headings, and the
version must not be lower than the newest existing release. Its major version must be
higher than that of the newest earlier release exactly when the section has Breaking
changes entries. Otherwise the job fails. A heading that looks like a version but is
not a level-two heading also fails. Older sections are not checked. No new version
can be released while `## [Unreleased]` has Breaking changes entries, including a
pending version published by a manual run: the commit being released already
contains those changes, so they belong in its section. The test suite applies the
same major-version rule to the two newest release sections of `CHANGELOG.md`, and
checks that each Breaking changes entry under `## [Unreleased]` and in the newest
release links an existing UPGRADING.md heading, so a release pull request that gets
either wrong fails before it merges.

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
