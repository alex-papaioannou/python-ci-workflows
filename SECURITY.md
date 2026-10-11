# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through
[GitHub security advisories](https://github.com/alex-papaioannou/python-ci-workflows/security/advisories/new),
not in public issues. Include the affected ref, the inputs used, and steps to
reproduce. Expect an acknowledgement within a week.

## Supported versions

Only the latest release of the current major version receives fixes.

## Hardening built into the workflow

- `permissions: contents: read`, and checkout does not persist credentials.
- Third-party actions are pinned to full commit SHAs.
- Requirements install with `--require-hashes` by default.
- Inputs reach scripts through environment variables, never by interpolation into
  code, and are validated before any project code runs.
