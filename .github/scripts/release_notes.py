"""Print the newest CHANGELOG.md release as GitHub Actions outputs.

The first level-two heading other than ``## [Unreleased]`` is the release to
publish. It must read ``## [X.Y.Z]`` or ``## [X.Y.Z] - YYYY-MM-DD`` and have
release notes, which are written to NOTES_PATH. A malformed version heading
fails loudly instead of silently falling through to an older release.

Usage: release_notes.py NOTES_PATH [RELEASED_TAGS_PATH [PREVIOUS_CHANGELOG_PATH]]

RELEASED_TAGS_PATH lists existing release tags, one per line; the version must
not be lower than any of them, and its major version must be higher than that of
the newest earlier release exactly when the section has ``### Breaking changes``
entries. A version that is not yet released also fails while ``## [Unreleased]``
has Breaking changes entries, because this commit already contains them.
PREVIOUS_CHANGELOG_PATH is CHANGELOG.md as of the parent commit (it may be
missing or empty); the ``introduced`` output says whether this commit added the
version.

Relative links to UPGRADING.md in the notes are rewritten to point at the file
in the released tag, since GitHub resolves relative links in release notes
against the release page. The notes list the subsections that can change what an
existing caller's run does first, and end with the release commit (GITHUB_SHA),
a ``uses:`` line pinned to it and, when RELEASED_TAGS_PATH names an earlier
release, a link comparing the two.
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
FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})")
LIST_ITEM = re.compile(r"(?:[-*+]|[0-9]+[.)])[ \t]")
UPGRADE_GUIDE_LINK = re.compile(r"\]\((?:\./)?(UPGRADING\.md(?:#[^)\s]*)?)\)")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
# Subsections that can change what an existing caller's run does, in the order
# the release notes list them ahead of the others.
CALLER_IMPACT = ("breaking changes", "changed defaults", "behaviour changes")
WORKFLOW = ".github/workflows/python-ci.yml"


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


def sections(changelog: str) -> list[tuple[str, str]]:
    """Return (title, body) for each level-two section, in order."""
    changelog = changelog.replace("\r\n", "\n")
    headings = list(SECTION.finditer(changelog))
    ends = [heading.start() for heading in headings[1:]] + [len(changelog)]
    return [
        (heading.group(1).strip(), changelog[heading.end() : end].strip())
        for heading, end in zip(headings, ends)
    ]


def newest_release(changelog: str) -> tuple[str, str, str]:
    """Return (version, major tag, notes) for the first released section."""
    changelog = changelog.replace("\r\n", "\n")
    check_heading_levels(changelog)
    for title, notes in sections(changelog):
        if title.lower() == "[unreleased]":
            continue
        match = VERSION_HEADING.fullmatch(title)
        if not match:
            raise SystemExit(
                f"CHANGELOG.md heading '## {title}' is not '## [X.Y.Z]' or "
                "'## [X.Y.Z] - YYYY-MM-DD'"
            )
        if not has_notes(notes):
            raise SystemExit(f"CHANGELOG.md section '## {title}' has no release notes")
        return ".".join(match.groups()), f"v{match.group(1)}", notes + "\n"
    raise SystemExit("CHANGELOG.md has no '## [X.Y.Z]' release heading")


def fenced_lines(markdown: str) -> list[tuple[str, bool]]:
    """Pair each line with whether it is a code fence or inside a fenced block."""
    lines: list[tuple[str, bool]] = []
    fence = ""  # The marker that opened the current fenced block, or "".
    for line in markdown.replace("\r\n", "\n").splitlines():
        marker = FENCE.match(line)
        lines.append((line, bool(fence or marker)))
        if marker and not fence:
            fence = marker.group(1)
        elif marker and not line[marker.end() :].strip():
            # A closing fence repeats the opening character at least as often.
            fence = "" if marker.group(1).startswith(fence) else fence
    return lines


def subsection_entries(notes: str, title: str) -> list[list[str]]:
    """Return the lines of each entry under a section's subsections with this title.

    The title is compared in lower case. Each list item or paragraph is one entry,
    with its continuation lines, nested lists and code blocks; each line is
    stripped. Deeper headings stay inside the subsection.
    """
    entries: list[list[str]] = []
    level = 0  # Heading level of the matching subsection we are in, or 0.
    previous_blank = True
    for line, fenced in fenced_lines(notes):
        heading = None if fenced else ANY_HEADING.fullmatch(line)
        if heading:
            depth = len(line.lstrip(" ")) - len(line.lstrip(" ").lstrip("#"))
            if heading.group(1).strip().lower() == title:
                level = depth
            elif depth <= level:
                level = 0
            previous_blank = True
        elif not line.strip():
            previous_blank = previous_blank or not fenced
        elif level:
            new_entry = not fenced and (previous_blank or LIST_ITEM.match(line))
            if entries and (line[0].isspace() or not new_entry):
                entries[-1].append(line.strip())
            else:
                entries.append([line.strip()])
            previous_blank = False
    return entries


def breaking_changes(notes: str) -> list[str]:
    """Return the entries under a section's Breaking changes subsections.

    Each entry's lines are joined with spaces.
    """
    return [" ".join(lines) for lines in subsection_entries(notes, "breaking changes")]


def release_versions(released_tags: list[str]) -> list[tuple[int, ...]]:
    """Return the versions of the vX.Y.Z tags, ignoring others such as v1."""
    return [
        tuple(int(part) for part in match.groups())
        for tag in released_tags
        if (match := RELEASE_TAG.fullmatch(tag.strip()))
    ]


def release_before(version: str, released_tags: list[str]) -> tuple[int, ...] | None:
    """Return the newest released version lower than the version, if any."""
    current = tuple(int(part) for part in version.split("."))
    earlier = [tag for tag in release_versions(released_tags) if tag < current]
    return max(earlier) if earlier else None


def check_not_older(version: str, released_tags: list[str]) -> None:
    """Fail when the changelog version is lower than an existing release."""
    current = tuple(int(part) for part in version.split("."))
    released = release_versions(released_tags)
    if released and current < max(released):
        newest = "v" + ".".join(map(str, max(released)))
        raise SystemExit(
            f"CHANGELOG.md version {version} is lower than the newest release {newest}"
        )


def check_major_bump(version: str, notes: str, released_tags: list[str]) -> None:
    """Fail unless the major version rises exactly when there are breaking changes.

    The version is compared with the newest release before it, so re-running the
    release of a version that is already published passes.
    """
    current = tuple(int(part) for part in version.split("."))
    previous = release_before(version, released_tags)
    if previous is None:
        return
    name = "v" + ".".join(map(str, previous))
    breaking = bool(breaking_changes(notes))
    if breaking and current[0] <= previous[0]:
        raise SystemExit(
            f"CHANGELOG.md version {version} has 'Breaking changes' entries but keeps "
            f"the major version of {name}; release it as {previous[0] + 1}.0.0"
        )
    if not breaking and current[0] > previous[0]:
        raise SystemExit(
            f"CHANGELOG.md version {version} raises the major version from {name} "
            "but has no 'Breaking changes' entries; list them under '### Breaking "
            "changes' or keep the major version"
        )


def check_unreleased(version: str, changelog: str, released_tags: list[str]) -> None:
    """Fail when publishing a version would also ship unreleased breaking changes.

    Releases are made from main, which already contains every change listed under
    ``## [Unreleased]``. Re-reading a version that is already published passes.
    """
    current = tuple(int(part) for part in version.split("."))
    released = release_versions(released_tags)
    if current in released:
        return
    pending: list[str] = []
    for title, notes in sections(changelog):
        if title.lower() != "[unreleased]":
            break
        pending += breaking_changes(notes)
    if not pending:
        return
    previous = release_before(version, released_tags)
    fix = f"move them into the {version} section"
    if previous and current[0] <= previous[0]:
        fix += f" and release it as {previous[0] + 1}.0.0"
    raise SystemExit(
        "CHANGELOG.md has 'Breaking changes' entries under '## [Unreleased]', which "
        f"this commit already contains and would ship in {version}; {fix}"
    )


def impact_first(notes: str) -> str:
    """Move the subsections that can change existing callers' runs to the top.

    Text before the first ``###`` subsection stays first, and the other
    subsections keep their order.
    """
    preamble: list[str] = []
    subsections: list[tuple[str, list[str]]] = []
    for line, fenced in fenced_lines(notes):
        heading = None if fenced else ANY_HEADING.fullmatch(line)
        depth = len(line.lstrip(" ")) - len(line.lstrip(" ").lstrip("#"))
        if heading and depth == 3:
            subsections.append((heading.group(1).strip().lower(), [line]))
        elif subsections:
            subsections[-1][1].append(line)
        else:
            preamble.append(line)
    subsections.sort(
        key=lambda subsection: (
            CALLER_IMPACT.index(subsection[0])
            if subsection[0] in CALLER_IMPACT
            else len(CALLER_IMPACT)
        )
    )
    blocks = ["\n".join(preamble).strip()]
    blocks += ["\n".join(lines).rstrip() for _, lines in subsections]
    return "\n\n".join(block for block in blocks if block) + "\n"


def pinning_notes(
    version: str,
    commit: str,
    repository: str,
    server_url: str,
    previous: tuple[int, ...] | None,
) -> str:
    """Return the closing subsection: the release commit and a line pinned to it.

    Unless this is the first release, it ends with a link comparing the release
    with the previous one.
    """
    if not COMMIT_SHA.fullmatch(commit):
        raise SystemExit(f"GITHUB_SHA {commit!r} is not a full commit SHA")
    lines = [
        "### Pinning this release",
        "",
        f"This release is commit `{commit}`. To pin a caller to it, use this line:",
        "",
        "```yaml",
        f"    uses: {repository}/{WORKFLOW}@{commit} # v{version}",
        "```",
    ]
    if previous:
        tag = "v" + ".".join(map(str, previous))
        compare = f"{server_url}/{repository}/compare/{tag}...v{version}"
        lines += ["", f"**Full changelog**: {compare}"]
    return "\n".join(lines) + "\n"


def absolute_upgrade_links(notes: str, version: str, repository_url: str) -> str:
    """Point relative UPGRADING.md links at the file in the released tag."""
    return UPGRADE_GUIDE_LINK.sub(
        lambda link: f"]({repository_url}/blob/v{version}/{link.group(1)})", notes
    )


def introduced(version: str, previous_changelog: str) -> bool:
    """Return whether the previous changelog did not yet name this version."""
    try:
        return newest_release(previous_changelog)[0] != version
    except SystemExit:
        return True


def main() -> int:
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    version, major, notes = newest_release(changelog)
    outputs = {"version": version, "major": major}
    previous_release = None
    if len(sys.argv) > 2:
        released_tags = Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
        check_not_older(version, released_tags)
        check_major_bump(version, notes, released_tags)
        check_unreleased(version, changelog, released_tags)
        previous_release = release_before(version, released_tags)
    if len(sys.argv) > 3:
        previous = Path(sys.argv[3])
        text = previous.read_text(encoding="utf-8") if previous.is_file() else ""
        outputs["introduced"] = "true" if introduced(version, text) else "false"
    server_url = os.environ["GITHUB_SERVER_URL"]
    repository = os.environ["GITHUB_REPOSITORY"]
    notes = absolute_upgrade_links(
        impact_first(notes), version, f"{server_url}/{repository}"
    )
    # The release job publishes this commit, so the notes can name it; the
    # README's pinned line is updated in a pull request after the release.
    notes += "\n" + pinning_notes(
        version, os.environ["GITHUB_SHA"], repository, server_url, previous_release
    )
    Path(sys.argv[1]).write_text(notes, encoding="utf-8")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.writelines(f"{key}={value}\n" for key, value in outputs.items())
    print(f"Newest release in CHANGELOG.md: {version} (major tag {major})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
