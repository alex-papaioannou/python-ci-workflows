# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/alex-papaioannou/python-ci-workflows/security/advisories/new),
not in public issues. Include the affected ref, the inputs used, and steps to
reproduce. Expect an acknowledgement within a week.

If the reporting form is unavailable, open a public issue that asks for a private
contact and contains no vulnerability details.

## Supported versions

The latest release of the current major version receives fixes. The major tag (such
as `v1`) moves to every new release of that major version, so a caller pinned to
`@v1` picks up fixes, and any other change, automatically. To review each update
before it runs, pin a full commit SHA instead, as the README's quick start does; see
[Choosing a ref](README.md#choosing-a-ref) and
[Staying up to date](README.md#staying-up-to-date).

A fix listed under Security in the `## [Unreleased]` section of
[CHANGELOG.md](CHANGELOG.md) is released within 7 days of merging. Until then it is
only on `main`. A check in this repository runs every week and fails once such a
fix has waited longer, so a missed release is reported at most a week after the
deadline.

v1 receives no releases after v2.0.0. From v2 on, when a new major version is
released, the previous major version receives security fixes, as patch releases of
its latest release, for six months. The README's
[support policy](README.md#support-policy) has the details.

## What the workflow does and does not protect

- It uses no secrets. Jobs request only `permissions: contents: read`, and checkout
  does not persist credentials.
- Third-party actions are pinned to full commit SHAs.
- Inputs reach the step scripts through environment variables, never by
  interpolation into code, and are validated before any project code runs.
- `requirements-file` installs with `--require-hashes` by default (`require-hashes`),
  and uv lock files and `pylock.toml` carry their own hashes.
- Tools the workflow installs for you are pinned by version only, not by hash: uv,
  Ruff, mypy, coverage.py, pytest, build, twine and pip-audit, together with their
  dependencies. Ruff, mypy, coverage.py and pytest are installed into the project's
  interpreter. To hash-check one of them, pin it in your hashed `requirements-file`
  at the same version the workflow requests; the workflow's install then finds it
  already satisfied.
- In a call with `upload-dist`, setup-python restores and saves no pip cache, so an
  Actions cache entry that another workflow saved cannot reach the distributions you
  publish. A self-hosted runner that is not ephemeral still keeps pip's and uv's own
  caches between jobs, so run that call on GitHub-hosted or ephemeral runners. The
  workflow declares no `cache-mode`; set one on your calling job to limit its cache
  access further.
- pip-audit runs from its own temporary environment and audits the installed
  packages without importing them.
- The pre-test hook, `.github/actions/python-ci-pre-test/action.yml` in your
  repository, runs with the same trust as your test code: it comes from the same
  checkout, so a pull request runs its own version of the hook, as it runs its own
  tests. Unlike test code, an action can read `github.token`, which in this
  workflow has only `contents: read`, and through actions such as `actions/cache`
  it can restore and save Actions caches and upload artifacts. Every call runs the
  hook, so in a call with `upload-dist` a hook that restores a cache undoes the
  protection above: restore none in that call. A `cache-mode` on the calling job
  limits the hook's cache access too. The hook runs only after the inputs are
  validated, and no input adds a step or is run as code.
