"""Print the newest CHANGELOG.md release as GitHub Actions outputs.

The newest section must be headed ``## [X.Y.Z]``. Its body becomes the release
notes, written to the path given as the first argument.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HEADING = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]\s*$", re.MULTILINE)


def newest_release(changelog: str) -> tuple[str, str, str]:
    """Return (version, major tag, notes) for the first release section."""
    match = HEADING.search(changelog)
    if not match:
        raise SystemExit("CHANGELOG.md has no '## [X.Y.Z]' release heading")
    following = re.search(r"^## ", changelog[match.end() :], re.MULTILINE)
    end = match.end() + following.start() if following else len(changelog)
    version = ".".join(match.groups())
    return version, f"v{match.group(1)}", changelog[match.end() : end].strip() + "\n"


def main() -> int:
    version, major, notes = newest_release(
        Path("CHANGELOG.md").read_text(encoding="utf-8")
    )
    Path(sys.argv[1]).write_text(notes, encoding="utf-8")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"version={version}\nmajor={major}\n")
    print(f"Newest release in CHANGELOG.md: {version} (major tag {major})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
