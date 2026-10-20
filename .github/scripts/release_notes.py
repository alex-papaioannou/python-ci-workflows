"""Print the newest CHANGELOG.md release as GitHub Actions outputs.

The first level-two heading other than ``## [Unreleased]`` is the release to
publish. It must read ``## [X.Y.Z]`` or ``## [X.Y.Z] - YYYY-MM-DD`` and have
release notes, which are written to NOTES_PATH. A malformed version heading
fails loudly instead of silently falling through to an older release.

Usage: release_notes.py NOTES_PATH [RELEASED_TAGS_PATH [PREVIOUS_CHANGELOG_PATH]]

RELEASED_TAGS_PATH lists existing release tags, one per line; the version must
not be lower than any of them. PREVIOUS_CHANGELOG_PATH is CHANGELOG.md as of the
parent commit (it may be missing or empty); the ``introduced`` output says
whether this commit added the version.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

NUMBER = r"(0|[1-9][0-9]*)"
SECTION = re.compile(r"^## (.*)$", re.MULTILINE)
# Any Markdown heading, as GitHub renders it, to catch near-misses of the format.
ANY_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)[ \t#]*$", re.MULTILINE)
VERSION_HEADING = re.compile(
    rf"\[{NUMBER}\.{NUMBER}\.{NUMBER}\](?: - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})?",
    re.ASCII,
)
RELEASE_TAG = re.compile(rf"v{NUMBER}\.{NUMBER}\.{NUMBER}", re.ASCII)


def check_heading_levels(changelog: str) -> None:
    """Reject version-like headings that are not plain level-two headings."""
    for match in ANY_HEADING.finditer(changelog):
        title = match.group(1)
        looks_like_release = re.search(r"\[\s*v?\d|unreleased", title, re.IGNORECASE)
        if looks_like_release and match.group(0) != f"## {title}":
            raise SystemExit(
                f"CHANGELOG.md heading {match.group(0).strip()!r} must be a level-two "
                "heading at the start of the line, such as '## [X.Y.Z]'"
            )


def has_notes(body: str) -> bool:
    """Return whether a section says anything beyond empty subsection headings."""
    return any(
        line.strip() and not line.lstrip().startswith("#") for line in body.splitlines()
    )


def newest_release(changelog: str) -> tuple[str, str, str]:
    """Return (version, major tag, notes) for the first released section."""
    changelog = changelog.replace("\r\n", "\n")
    check_heading_levels(changelog)
    headings = list(SECTION.finditer(changelog))
    for index, heading in enumerate(headings):
        title = heading.group(1).strip()
        if title.lower() == "[unreleased]":
            continue
        match = VERSION_HEADING.fullmatch(title)
        if not match:
            raise SystemExit(
                f"CHANGELOG.md heading '## {title}' is not '## [X.Y.Z]' or "
                "'## [X.Y.Z] - YYYY-MM-DD'"
            )
        end = (
            headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
        )
        notes = changelog[heading.end() : end].strip()
        if not has_notes(notes):
            raise SystemExit(f"CHANGELOG.md section '## {title}' has no release notes")
        return ".".join(match.groups()), f"v{match.group(1)}", notes + "\n"
    raise SystemExit("CHANGELOG.md has no '## [X.Y.Z]' release heading")


def check_not_older(version: str, released_tags: list[str]) -> None:
    """Fail when the changelog version is lower than an existing release."""
    current = tuple(int(part) for part in version.split("."))
    released = [
        tuple(int(part) for part in match.groups())
        for tag in released_tags
        if (match := RELEASE_TAG.fullmatch(tag.strip()))
    ]
    if released and current < max(released):
        newest = "v" + ".".join(map(str, max(released)))
        raise SystemExit(
            f"CHANGELOG.md version {version} is lower than the newest release {newest}"
        )


def introduced(version: str, previous_changelog: str) -> bool:
    """Return whether the previous changelog did not yet name this version."""
    try:
        return newest_release(previous_changelog)[0] != version
    except SystemExit:
        return True


def main() -> int:
    version, major, notes = newest_release(
        Path("CHANGELOG.md").read_text(encoding="utf-8")
    )
    outputs = {"version": version, "major": major}
    if len(sys.argv) > 2:
        check_not_older(
            version, Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
        )
    if len(sys.argv) > 3:
        previous = Path(sys.argv[3])
        text = previous.read_text(encoding="utf-8") if previous.is_file() else ""
        outputs["introduced"] = "true" if introduced(version, text) else "false"
    Path(sys.argv[1]).write_text(notes, encoding="utf-8")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.writelines(f"{key}={value}\n" for key, value in outputs.items())
    print(f"Newest release in CHANGELOG.md: {version} (major tag {major})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
