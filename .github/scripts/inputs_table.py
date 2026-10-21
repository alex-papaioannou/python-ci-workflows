"""Check or rewrite the generated columns of the README inputs tables.

Type and Default come from the inputs that .github/workflows/python-ci.yml
declares. Added in names the CHANGELOG.md section whose Added list introduces the
input with a bullet that starts with '`<name>` input': the version, or Unreleased
for the Unreleased section. The 1.0.0 section names no inputs that way, so the
inputs 1.0.0 declared are listed here instead, and any other input without such a
bullet fails.

Usage: inputs_table.py [--write]

Without --write, print each cell that differs and exit 1 if any does. With
--write, rewrite those cells in README.md. The Input and Description columns are
written by hand; a declared input without a row, or a row for an input that is not
declared, fails in both modes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ".github/workflows/python-ci.yml"
HEADER = ("Input", "Type", "Default", "Added in", "Description")
FIRST_RELEASE = "1.0.0"
# The inputs python-ci.yml declared at the v1.0.0 tag, bb72b4b.
FIRST_RELEASE_INPUTS = frozenset(
    {
        "allow-prereleases",
        "audit-ignore",
        "audit-version",
        "build-package",
        "cancel-in-progress",
        "compile-exclude",
        "compile-paths",
        "coverage-fail-under",
        "coverage-version",
        "doctest-paths",
        "experimental-python-versions",
        "hash-seed",
        "install-project",
        "installer",
        "junit-file",
        "lint-paths",
        "max-parallel",
        "mypy-version",
        "operating-systems",
        "project-extras",
        "pytest-args",
        "pytest-version",
        "python-versions",
        "require-hashes",
        "requirements-file",
        "ruff-format",
        "ruff-version",
        "test-directory",
        "test-fail-fast",
        "test-pattern",
        "test-runner",
        "test-verbosity",
        "test-wheel",
        "timeout-minutes",
        "top-level-directory",
        "uv-version",
        "warnings-as-errors",
        "working-directory",
    }
)
SECTION = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)
SUBSECTION = re.compile(r"^### ", re.MULTILINE)
INTRODUCTION = re.compile(r"^- `([A-Za-z0-9_.-]+)` input\b", re.MULTILINE)
# A cell ends at a pipe that is not escaped, as in GitHub's tables.
CELL_BORDER = re.compile(r"(?<!\\)\|")
NAME_CELL = re.compile(r"`([A-Za-z0-9_.-]+)`")


def code(text: str) -> str:
    """Return text as a code span that keeps its meaning inside a table cell."""
    fence = "`" * (max(map(len, re.findall("`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}".replace("|", "\\|")


def declared(workflow: str) -> dict[str, tuple[str, str]]:
    """Return the (Type, Default) cells of each input python-ci.yml declares."""
    document = yaml.safe_load(workflow)
    # PyYAML reads the bare key "on" as the boolean True.
    inputs = document.get(True, document.get("on"))["workflow_call"]["inputs"]
    cells: dict[str, tuple[str, str]] = {}
    for name, spec in inputs.items():
        default = spec.get("default")
        if not isinstance(spec.get("type"), str) or default is None:
            raise SystemExit(f"{WORKFLOW}: input {name} needs a type and a default")
        if isinstance(default, bool):
            text = "true" if default else "false"
        else:
            text = str(default) if default != "" else '""'
        cells[name] = (spec["type"], code(text))
    return cells


def introductions(changelog: str) -> dict[str, str]:
    """Map each input an Added bullet introduces to the section that does."""
    found: dict[str, str] = {}
    headings = list(SECTION.finditer(changelog))
    ends = [heading.start() for heading in headings[1:]] + [len(changelog)]
    for heading, end in zip(headings, ends):
        for subsection in SUBSECTION.split(changelog[heading.end() : end])[1:]:
            title, _, entries = subsection.partition("\n")
            if title.strip().lower() != "added":
                continue
            for name in INTRODUCTION.findall(entries):
                if name in found:
                    raise SystemExit(
                        f"CHANGELOG.md introduces the {name} input under both "
                        f"{found[name]} and {heading.group(1)}"
                    )
                found[name] = heading.group(1)
    return found


def expected_cells(workflow: str, changelog: str) -> dict[str, tuple[str, ...]]:
    """Return the Type, Default and Added in cells each input's row should have."""
    inputs = declared(workflow)
    added = introductions(changelog)
    for name, section in added.items():
        first = name in FIRST_RELEASE_INPUTS
        if first != (section == FIRST_RELEASE):
            then = "already declared" if first else "did not declare"
            raise SystemExit(
                f"CHANGELOG.md introduces the {name} input under {section}, but "
                f"{WORKFLOW} {then} it at v{FIRST_RELEASE}"
            )
        if section.lower() == "unreleased" and name not in inputs:
            raise SystemExit(
                f"CHANGELOG.md introduces the {name} input under {section}, but "
                f"{WORKFLOW} declares no such input"
            )
    for name in inputs:
        if name not in added and name not in FIRST_RELEASE_INPUTS:
            raise SystemExit(
                f"{WORKFLOW} declares the {name} input, but no bullet in an Added "
                f"subsection of CHANGELOG.md starts with '`{name}` input'; add one "
                "under Unreleased"
            )
    return {
        name: (*cells, added.get(name, FIRST_RELEASE)) for name, cells in inputs.items()
    }


def split_row(line: str) -> list[str]:
    """Return the cells of a table row, without their surrounding spaces."""
    return [cell.strip() for cell in CELL_BORDER.split(line.strip())[1:-1]]


def update(
    readme: str, expected: dict[str, tuple[str, ...]]
) -> tuple[str, list[str], list[str]]:
    """Return the README with every generated cell rewritten.

    Also returns the cells that changed, and the problems that rewriting cannot
    fix, such as an input that has no row.
    """
    lines = readme.splitlines(keepends=True)
    changes: list[str] = []
    problems: list[str] = []
    seen: set[str] = set()
    in_table = False
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            in_table = False
            continue
        cells = split_row(line)
        if tuple(cells) == HEADER:
            in_table = True
            continue
        if not in_table or set("".join(cells)) <= set("-: "):
            continue
        name = NAME_CELL.fullmatch(cells[0])
        if len(cells) != len(HEADER) or not name:
            problems.append(
                f"README.md line {index + 1} is not an input row: "
                f"{len(HEADER)} cells, the first an input name in backticks"
            )
            continue
        if name.group(1) not in expected:
            problems.append(
                f"README.md documents {name.group(1)}, which {WORKFLOW} does not declare"
            )
            continue
        if name.group(1) in seen:
            problems.append(f"README.md documents {name.group(1)} twice")
            continue
        seen.add(name.group(1))
        fixed = cells.copy()
        for column, want in zip(HEADER[1:4], expected[name.group(1)]):
            have = cells[HEADER.index(column)]
            if have != want:
                changes.append(
                    f"README.md: {column} of {name.group(1)} is {have or 'empty'}, "
                    f"not {want}"
                )
                fixed[HEADER.index(column)] = want
        if fixed != cells:
            ending = line[len(line.rstrip("\r\n")) :]
            lines[index] = "| " + " | ".join(fixed) + " |" + ending
    problems += [
        f"README.md has no row for the {name} input in a table headed "
        f"'| {' | '.join(HEADER)} |'"
        for name in expected
        if name not in seen
    ]
    return "".join(lines), changes, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check or rewrite the generated columns of the README inputs tables."
    )
    parser.add_argument(
        "--write", action="store_true", help="rewrite the cells in README.md"
    )
    write = parser.parse_args(argv).write
    expected = expected_cells(
        (ROOT / WORKFLOW).read_text(encoding="utf-8"),
        (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
    )
    readme = ROOT / "README.md"
    # Bytes, so that --write keeps the line endings of the checkout.
    text, changes, problems = update(readme.read_bytes().decode("utf-8"), expected)
    for message in problems + changes:
        print(message, file=sys.stderr)
    if problems:
        return 1
    if write:
        if changes:
            readme.write_text(text, encoding="utf-8", newline="")
        print(f"Updated {len(changes)} cells in README.md")
        return 0
    if changes:
        print(
            "Run python .github/scripts/inputs_table.py --write to update README.md",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
