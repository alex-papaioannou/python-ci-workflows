"""Fail when a release is overdue.

SECURITY.md promises that a fix listed under ``### Security`` in the
``## [Unreleased]`` section of CHANGELOG.md is released within seven days of
merging, and CONTRIBUTING that at most four weeks pass between releases while
``## [Unreleased]`` has entries. A release empties ``## [Unreleased]``, so the
oldest pending entry merged in the commit that started the newest run of
CHANGELOG.md versions in which the section, or its Security subsection, has
entries. This walks back through the versions on HEAD's first-parent history to
find that commit and fails when it is too old. Rewording a pending entry does not
restart its clock, because the run goes on. It needs the whole history, as a
clone with ``fetch-depth: 0`` has.

Usage: release_cadence.py
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import takewhile
from typing import NamedTuple

from release_notes import has_notes, sections, subsection_entries

DAYS = 7  # Longest wait for a fix listed under Security.
WEEKS = 4  # Longest wait for any other entry.


class Version(NamedTuple):
    """CHANGELOG.md as a commit left it, with the commit's committer date."""

    commit: str
    date: datetime
    changelog: str


def unreleased(changelog: str) -> str:
    """Return the notes of the ## [Unreleased] section, or "" without one."""
    found = sections(changelog)
    return found[0][1] if found and found[0][0].lower() == "[unreleased]" else ""


def pending_security_fixes(changelog: str) -> list[str]:
    """Return the first line of each entry under Security in ## [Unreleased]."""
    return [lines[0] for lines in subsection_entries(unreleased(changelog), "security")]


def overdue(versions: Iterable[Version], now: datetime) -> list[str]:
    """Return a problem for each promise that the pending entries have outlived.

    versions are CHANGELOG.md as each commit on HEAD's first-parent history that
    changed it left it, newest first. Reading stops at the newest version whose
    ## [Unreleased] is empty: the oldest version read started the run of versions
    with entries there, and a Security entry is an entry there too.
    """
    entries = list(takewhile(lambda v: has_notes(unreleased(v.changelog)), versions))
    fixes = list(takewhile(lambda v: pending_security_fixes(v.changelog), entries))
    problems = []
    if fixes and now - fixes[-1].date > timedelta(days=DAYS):
        listed = "; ".join(map(repr, pending_security_fixes(fixes[0].changelog)))
        problems.append(
            "'## [Unreleased]' has listed a fix under Security since commit "
            f"{fixes[-1].commit[:7]} on {fixes[-1].date:%Y-%m-%d}, more than {DAYS} "
            f"days ago. SECURITY.md promises a release within {DAYS} days of "
            f"merging; open a release pull request. Pending: {listed}"
        )
    if entries and now - entries[-1].date > timedelta(weeks=WEEKS):
        problems.append(
            "'## [Unreleased]' has had entries since commit "
            f"{entries[-1].commit[:7]} on {entries[-1].date:%Y-%m-%d}, more than "
            f"{WEEKS} weeks ago. CONTRIBUTING allows at most {WEEKS} weeks between "
            "releases while it has entries; open a release pull request"
        )
    return problems


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise SystemExit(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def history() -> Iterator[Version]:
    """Yield the versions of CHANGELOG.md on HEAD's first-parent history, newest first.

    On that history a pull request is its merge commit, or the commits a squash or
    rebase merge created, so the committer date is when the pull request merged, not
    when its branch's commits were made.
    """
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        raise SystemExit(
            "This is a shallow clone, so the commits that added the pending entries "
            "may be missing; check out with fetch-depth: 0"
        )
    # :(top) names the file at the top of the repository from any directory.
    log = git("log", "--first-parent", "--format=%H %cI", "--", ":(top)CHANGELOG.md")
    if not log.strip():
        raise SystemExit("No commit on HEAD's first-parent history has CHANGELOG.md")
    for line in log.splitlines():
        commit, date = line.split()
        changelog = git("show", f"{commit}:CHANGELOG.md")
        yield Version(commit, datetime.fromisoformat(date), changelog)


def main() -> int:
    problems = overdue(history(), datetime.now(UTC))
    for problem in problems:
        # Workflow commands read %, CR and LF as escapes.
        print(f"::error file=CHANGELOG.md::{problem.replace('%', '%25')}")
    if not problems:
        print(
            f"No release is overdue: no fix under Security in '## [Unreleased]' has "
            f"waited over {DAYS} days, and no entry over {WEEKS} weeks"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
