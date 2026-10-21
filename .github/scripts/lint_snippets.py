"""Lint the workflow examples in the documentation with actionlint and zizmor.

Every YAML code block in README.md and docs/*.md with a top-level ``jobs:`` key,
and every example caller in examples/*/.github/workflows, is written to a scratch
tree and checked there as a workflow file, as workflow-lint.yml checks this
repository's own workflows. A block without a top-level ``on:`` key shows only
jobs, so it gets a minimal header first, without the keys it already has:

    on: push
    permissions:
      contents: read

A block may accept the findings of one zizmor audit or actionlint rule, named by
a marker on the line directly above its opening fence:

    <!-- lint-snippets: accept unpinned-uses -->

zizmor ignores its own ignore comments here, so the marker is the only way to
accept a finding. A marker anywhere else, or one whose block has no such finding,
fails. zizmor runs its online audits when GH_TOKEN holds a GitHub token, as for
the workflow files.

Usage: lint_snippets.py
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
# The opening or closing line of a fenced code block, and its info string.
FENCE = re.compile(r"([ \t]*)(`{3,}|~{3,})[ \t]*([^`\s]*)")
MARKER = re.compile(
    r"[ \t]*<!--[ \t]*lint-snippets:[ \t]*accept[ \t]+([\w-]+)[ \t]*-->"
)
JOBS = re.compile(r"^jobs[ \t]*:", re.MULTILINE)
ON = re.compile(r"""^["']?on["']?[ \t]*:""", re.MULTILINE)
PERMISSIONS = re.compile(r"^permissions[ \t]*:", re.MULTILINE)
MISPLACED = "a lint-snippets marker must be on the line directly above a code block"
NOT_LINTED = (
    "the block below this lint-snippets marker is not linted: it is not YAML with a "
    "top-level jobs: key"
)
ZIZMOR = (
    "zizmor",
    "--format=json",
    "--strict-collection",
    "--min-severity=medium",
    # Findings are accepted only through markers, never through ignore comments.
    "--no-ignores",
    "--no-config",
    "--no-exit-codes",
)


class Snippet(NamedTuple):
    """A workflow to lint, and where its lines come from."""

    source: str  # The file it comes from, relative to the repository.
    line: int  # The line of source that holds its first line.
    text: str  # The workflow as linted, header included.
    header: int  # The number of header lines added before the first line.
    accept: str  # The audit or rule whose findings it accepts, or "".


class Finding(NamedTuple):
    """A finding of a linter in a scratch file."""

    path: str
    line: int
    name: str  # The zizmor audit or actionlint rule.
    message: str


class Problem(NamedTuple):
    """A problem to report at a line of a repository file."""

    source: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.message}"


class OpenBlock(NamedTuple):
    """A fenced code block whose closing fence is still to come."""

    indent: int  # Spaces before the opening fence.
    fence: str
    language: str
    line: int  # The line of the opening fence.
    accept: str
    lines: list[str]


def wrap(source: str, line: int, text: str, accept: str = "") -> Snippet:
    """Return a snippet of a block, with a header when it shows only jobs."""
    header = ""
    if not ON.search(text):
        header = "on: push\n"
        if not PERMISSIONS.search(text):
            header += "permissions:\n  contents: read\n"
    return Snippet(source, line, header + text, header.count("\n"), accept)


def markdown_snippets(
    source: str, markdown: str
) -> tuple[list[Snippet], list[Problem]]:
    """Return the snippets of a Markdown file and the problems with its markers.

    A block of any language that never closes runs to the end of the file and
    hides the blocks after it, so it is a problem too.
    """
    snippets: list[Snippet] = []
    problems: list[Problem] = []
    marker: tuple[int, str] | None = None  # The line and name of a marker.
    block: OpenBlock | None = None
    for number, line in enumerate(markdown.splitlines(), 1):
        if block is not None:
            closing = FENCE.fullmatch(line.rstrip())
            # A closing fence repeats the opening character at least as often.
            if (
                closing
                and not closing.group(3)
                and closing.group(2).startswith(block.fence)
            ):
                text = "".join(block.lines)
                if block.language.lower() in ("yaml", "yml") and JOBS.search(text):
                    snippets.append(wrap(source, block.line + 1, text, block.accept))
                elif block.accept:
                    problems.append(Problem(source, block.line - 1, NOT_LINTED))
                block = None
            else:
                # The content loses up to as many spaces as the fence is indented.
                spaces = len(line) - len(line.lstrip(" "))
                block.lines.append(line[min(spaces, block.indent) :] + "\n")
            continue
        opening = FENCE.match(line)
        if marker and not opening:
            problems.append(Problem(source, marker[0], MISPLACED))
        if opening:
            block = OpenBlock(
                len(opening.group(1)),
                opening.group(2),
                opening.group(3),
                number,
                marker[1] if marker else "",
                [],
            )
            marker = None
        else:
            found = MARKER.fullmatch(line.rstrip())
            marker = (number, found.group(1)) if found else None
    if marker:
        problems.append(Problem(source, marker[0], MISPLACED))
    if block is not None:
        problems.append(
            Problem(source, block.line, "the code block has no closing fence")
        )
    return snippets, problems


def collect(root: Path) -> tuple[list[Snippet], list[Problem]]:
    """Return the snippets of the documentation and the example callers."""
    snippets: list[Snippet] = []
    problems: list[Problem] = []
    for path in [root / "README.md", *sorted(root.glob("docs/*.md"))]:
        found = markdown_snippets(
            path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")
        )
        snippets += found[0]
        problems += found[1]
    callers = [*root.glob("examples/*/.github/workflows/*.yml")]
    callers += root.glob("examples/*/.github/workflows/*.yaml")
    # A caller is a complete workflow, linted as it is.
    snippets += [
        Snippet(path.relative_to(root).as_posix(), 1, path.read_text("utf-8"), 0, "")
        for path in sorted(callers)
    ]
    return snippets, problems


def run(command: list[str], cwd: str, findings_status: int = 0) -> str:
    """Run a linter and return its output; exit when the linter itself fails."""
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, encoding="utf-8", check=False
        )
    except FileNotFoundError:
        raise SystemExit(
            f"{command[0]} is not installed; install requirements-lint.txt"
        ) from None
    if result.returncode not in (0, findings_status):
        raise SystemExit(
            f"{command[0]} failed with exit status {result.returncode}:\n"
            f"{result.stderr.strip()}"
        )
    return result.stdout


def actionlint_findings(output: str) -> list[Finding]:
    """Read the findings that actionlint -format '{{json .}}' printed."""
    return [
        Finding(error["filepath"], error["line"], error["kind"], error["message"])
        for error in json.loads(output)
    ]


def zizmor_findings(output: str) -> list[Finding]:
    """Read the findings that zizmor --format=json printed, at their primary spot."""
    findings = []
    for finding in json.loads(output):
        locations = finding["locations"]
        primary = next(
            (spot for spot in locations if spot["symbolic"]["kind"] == "Primary"),
            locations[0],
        )
        path = primary["symbolic"]["key"]["Local"]["verbatim_path"]
        message = finding["desc"]
        if primary["symbolic"].get("annotation"):
            message += f" ({primary['symbolic']['annotation']})"
        row = primary["concrete"]["location"]["start_point"]["row"]
        findings.append(Finding(path, row + 1, finding["ident"], message))
    return findings


def scratch_name(snippet: Snippet) -> str:
    """Return a file name for the snippet that says where it comes from."""
    return re.sub(r"[^\w.-]+", "-", f"{snippet.source}-L{snippet.line}") + ".yml"


def check(
    snippets: list[Snippet], findings: list[Finding], complete: bool = True
) -> list[Problem]:
    """Return a problem for each finding that its snippet does not accept.

    When the findings are complete, a snippet that accepts findings it does not
    have is a problem too.
    """
    names = {scratch_name(snippet): snippet for snippet in snippets}
    problems = []
    accepted = set()
    for finding in findings:
        snippet = names[PurePosixPath(finding.path).name]
        if finding.name == snippet.accept:
            accepted.add(snippet)
            continue
        # A finding in the header points at the snippet's first line.
        line = snippet.line + max(finding.line - 1 - snippet.header, 0)
        problems.append(
            Problem(snippet.source, line, f"{finding.name}: {finding.message}")
        )
    problems += [
        Problem(
            snippet.source,
            snippet.line - 2,  # The marker's line, above the opening fence.
            f"the block accepts {snippet.accept}, but has no such finding",
        )
        for snippet in snippets
        if complete and snippet.accept and snippet not in accepted
    ]
    return problems


def lint(snippets: list[Snippet]) -> list[Problem]:
    """Lint the snippets as workflow files in a scratch tree."""
    with tempfile.TemporaryDirectory(prefix="lint-snippets-") as scratch:
        workflows = Path(scratch, ".github", "workflows")
        workflows.mkdir(parents=True)
        paths = []
        for snippet in snippets:
            name = scratch_name(snippet)
            (workflows / name).write_text(snippet.text, encoding="utf-8")
            paths.append(f".github/workflows/{name}")
        # actionlint exits 1 when it has findings.
        output = run(["actionlint", "-format", "{{json .}}", *paths], scratch, 1)
        findings = actionlint_findings(output)
        try:
            findings += zizmor_findings(run([*ZIZMOR, *paths], scratch))
        except SystemExit:
            # zizmor stops at a file it cannot parse or audit, and names it by its
            # scratch path. Report what actionlint found, such as a block that does
            # not parse, at the line it comes from before zizmor's error.
            annotate(check(snippets, findings, complete=False))
            raise
    return check(snippets, findings)


def annotate(problems: list[Problem]) -> None:
    """Print each problem as an error annotation at its line."""
    for problem in problems:
        # Workflow commands read %, CR and LF as escapes.
        message = problem.message.replace("%", "%25").replace("\r", "%0D")
        message = message.replace("\n", "%0A")
        print(f"::error file={problem.source},line={problem.line}::{message}")


def main() -> int:
    snippets, problems = collect(ROOT)
    # Report these even when a linter fails.
    annotate(problems)
    found = lint(snippets)
    annotate(found)
    if problems or found:
        return 1
    print(
        f"actionlint and zizmor found nothing in {len(snippets)} workflow examples "
        "and example callers"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
