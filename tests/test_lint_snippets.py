"""Tests for the linter of the workflow examples in the documentation."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - requirements-dev.txt installs PyYAML
    yaml = None  # type: ignore[assignment]

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".github/scripts/lint_snippets.py"
SAMPLE = ROOT / "tests/fixtures/snippets.md"
spec = importlib.util.spec_from_file_location("lint_snippets", SCRIPT)
assert spec and spec.loader
lint_snippets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint_snippets)
HEADER = "on: push\npermissions:\n  contents: read\n"
PINNED = (
    "    uses: owner/repo/.github/workflows/python-ci.yml@"
    "0123456789abcdef0123456789abcdef01234567 # v1.1.0\n"
)


def actionlint_error(path, line, kind, message):
    """Return an error as actionlint's -format '{{json .}}' prints it."""
    return {"message": message, "filepath": path, "line": line, "kind": kind}


def zizmor_finding(path, row, ident, desc, annotation):
    """Return a finding as zizmor's --format=json prints it; row counts from 0."""

    def location(kind, row, annotation):
        return {
            "symbolic": {
                "key": {"Local": {"verbatim_path": path}},
                "annotation": annotation,
                "kind": kind,
            },
            "concrete": {"location": {"start_point": {"row": row, "column": 4}}},
        }

    return {
        "ident": ident,
        "desc": desc,
        "locations": [
            location("Hidden", row - 1, "this job"),
            location("Primary", row, annotation),
        ],
        "ignored": False,
    }


class ExtractionTests(unittest.TestCase):
    def test_the_sample_yields_each_block_with_jobs_wrapped_or_as_it_is(self):
        snippets, problems = lint_snippets.markdown_snippets(
            "sample.md", SAMPLE.read_text(encoding="utf-8")
        )
        self.assertEqual(problems, [])
        self.assertEqual(
            [(s.source, s.line, s.header, s.accept) for s in snippets],
            [
                # A complete workflow keeps its own on: and permissions:.
                ("sample.md", 6, 0, ""),
                # A job fragment is wrapped in the minimal header.
                ("sample.md", 18, 3, ""),
                # A fragment with permissions: gets only on:.
                ("sample.md", 26, 1, ""),
                # The block under the marker accepts the finding it names.
                ("sample.md", 37, 3, "unpinned-uses"),
                # A block in a list item.
                ("sample.md", 64, 3, ""),
            ],
        )
        complete, fragment, permissions, accepting, listed = snippets
        self.assertTrue(complete.text.startswith("name: CI\non: push\n"))
        self.assertEqual(fragment.text, f"{HEADER}jobs:\n  python:\n{PINNED}")
        self.assertEqual(
            permissions.text,
            f"on: push\npermissions:\n  contents: read\njobs:\n  python:\n{PINNED}",
        )
        self.assertTrue(accepting.text.endswith("python-ci.yml@v1\n"))
        # The list item's indentation is not part of the workflow.
        self.assertEqual(
            listed.text,
            f"{HEADER}jobs:\n  python:\n"
            "    uses: owner/repo/.github/workflows/python-ci.yml@v1\n",
        )

    def test_a_marker_counts_only_directly_above_a_linted_block(self):
        block = "```yaml\njobs:\n  a:\n    uses: o/r/.github/workflows/w.yml@v1\n```\n"
        marker = "<!-- lint-snippets: accept unpinned-uses -->\n"
        for markdown, line, message in (
            (f"Text.\n\n{marker}\n{block}", 3, lint_snippets.MISPLACED),
            (f"{marker}Text.\n\n{block}", 1, lint_snippets.MISPLACED),
            (f"{block}\n{marker}", 7, lint_snippets.MISPLACED),
            (
                f"{marker}```sh\njobs:\n```\n",
                1,
                lint_snippets.NOT_LINTED,
            ),
            (
                f"{marker}```yaml\n    with:\n      a: 1\n```\n",
                1,
                lint_snippets.NOT_LINTED,
            ),
            # A block that never closes would hide the blocks after it.
            (f"Text.\n\n~~~sh\n{block}", 3, "the code block has no closing fence"),
        ):
            with self.subTest(markdown=markdown):
                snippets, problems = lint_snippets.markdown_snippets("DOC.md", markdown)
                self.assertEqual(
                    problems, [lint_snippets.Problem("DOC.md", line, message)]
                )
                for snippet in snippets:
                    self.assertEqual(snippet.accept, "")
        # Two markers cannot both be directly above a block.
        snippets, problems = lint_snippets.markdown_snippets(
            "DOC.md", marker + marker.replace("unpinned-uses", "expression") + block
        )
        self.assertEqual(
            problems, [lint_snippets.Problem("DOC.md", 1, lint_snippets.MISPLACED)]
        )
        self.assertEqual([s.accept for s in snippets], ["expression"])

    def test_the_repository_examples_are_collected_with_the_callers(self):
        snippets, problems = lint_snippets.collect(ROOT)
        self.assertEqual(problems, [])
        sources = {snippet.source for snippet in snippets}
        callers = {
            f"examples/{name}/.github/workflows/ci.yml"
            for name in ("stdlib-app", "src-package", "uv-project")
        }
        self.assertLessEqual(callers, sources)
        # Only the major tag's snippet accepts a finding: its unpinned reference.
        (accepting,) = [snippet for snippet in snippets if snippet.accept]
        self.assertEqual(accepting.accept, "unpinned-uses")
        self.assertIn("python-ci.yml@v1\n", accepting.text)
        # The quick start and its pytest variant are complete workflows.
        complete = [s for s in snippets if s.source == "README.md" and not s.header]
        self.assertEqual(len(complete), 2)


class CheckTests(unittest.TestCase):
    def setUp(self):
        markdown = (
            "<!-- lint-snippets: accept unpinned-uses -->\n"
            "```yaml\njobs:\n  a:\n    uses: o/r/.github/workflows/w.yml@v1\n```\n\n"
            "```yaml\non: push\njobs:\n  b:\n    runs-on: ubuntu-latest\n```\n"
        )
        self.snippets, _ = lint_snippets.markdown_snippets("DOC.md", markdown)
        self.wrapped, self.complete = (
            ".github/workflows/" + lint_snippets.scratch_name(snippet)
            for snippet in self.snippets
        )

    def test_findings_point_at_the_markdown_lines_and_markers_accept_one_name(self):
        findings = lint_snippets.actionlint_findings(
            json.dumps(
                [
                    actionlint_error(self.complete, 2, "syntax-check", "no steps"),
                    # A finding in the header points at the block's first line.
                    actionlint_error(self.wrapped, 1, "events", "bad event"),
                ]
            )
        )
        findings += lint_snippets.zizmor_findings(
            json.dumps(
                [
                    zizmor_finding(
                        "./" + self.wrapped,
                        5,
                        "unpinned-uses",
                        "unpinned",
                        "not a hash",
                    ),
                    zizmor_finding(
                        self.wrapped, 5, "unpinned-uses", "unpinned", "not a hash"
                    ),
                    zizmor_finding(
                        self.wrapped, 5, "ref-confusion", "confusable", "tag and branch"
                    ),
                ]
            )
        )
        self.assertEqual(
            [str(problem) for problem in lint_snippets.check(self.snippets, findings)],
            [
                "DOC.md:10: syntax-check: no steps",
                "DOC.md:3: events: bad event",
                "DOC.md:5: ref-confusion: confusable (tag and branch)",
            ],
        )

    def test_a_marker_without_its_finding_fails(self):
        self.assertEqual(
            lint_snippets.check(self.snippets, []),
            [
                lint_snippets.Problem(
                    "DOC.md",
                    1,
                    "the block accepts unpinned-uses, but has no such finding",
                )
            ],
        )

    @unittest.skipUnless(
        shutil.which("actionlint") and shutil.which("zizmor"),
        "needs actionlint and zizmor from requirements-lint.txt",
    )
    def test_the_linters_flag_the_sample_snippet_without_a_marker(self):
        snippets, _ = lint_snippets.markdown_snippets(
            "sample.md", SAMPLE.read_text(encoding="utf-8")
        )
        with unittest.mock.patch.dict(os.environ, {"ZIZMOR_OFFLINE": "true"}):
            problems = lint_snippets.lint(snippets)
        (problem,) = problems
        self.assertEqual(problem.source, "sample.md")
        self.assertEqual(problem.line, 66)
        self.assertTrue(problem.message.startswith("unpinned-uses: "), problem)


class MainTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "docs").mkdir()
        shutil.copyfile(SAMPLE, self.root / "README.md")
        (self.root / "docs/guide.md").write_text(
            "Text.\n\n<!-- lint-snippets: accept expression -->\n", encoding="utf-8"
        )
        caller = self.root / "examples/app/.github/workflows/ci.yml"
        caller.parent.mkdir(parents=True)
        caller.write_text(f"on: push\njobs:\n  python:\n{PINNED}", encoding="utf-8")

    def main(self, *findings, actionlint=(), zizmor_error=""):
        """Run main with fake linters; return its status, output and their commands.

        actionlint finds the error that each of actionlint, a function of the
        scratch paths, returns. zizmor fails with zizmor_error when it is set, and
        otherwise finds the unpinned reference the sample's marker accepts and the
        finding that each of findings, a function of the scratch paths, returns.
        """
        commands = []

        def zizmor(paths):
            if zizmor_error:
                raise SystemExit(zizmor_error)
            (accepted,) = [path for path in paths if path.endswith("-L37.yml")]
            found = [zizmor_finding(accepted, 5, "unpinned-uses", "unpinned", "")]
            found += [finding(paths) for finding in findings]
            return json.dumps(found)

        def run(command, cwd, findings_status=0):
            paths = [arg for arg in command if arg.startswith(".github/workflows/")]
            for path in paths:
                self.assertTrue(Path(cwd, path).is_file(), path)
            commands.append(command)
            if command[0] == "zizmor":
                return zizmor(paths)
            return json.dumps([error(paths) for error in actionlint])

        output = io.StringIO()
        with (
            unittest.mock.patch.object(lint_snippets, "ROOT", self.root),
            unittest.mock.patch.object(lint_snippets, "run", run),
            contextlib.redirect_stdout(output),
        ):
            try:
                status = lint_snippets.main()
            except SystemExit as failure:
                status = failure.code
        return status, output.getvalue(), commands

    def test_findings_and_misplaced_markers_fail_as_annotations(self):
        def listed(paths):
            (path,) = [path for path in paths if path.endswith("-L64.yml")]
            return zizmor_finding(path, 5, "unpinned-uses", "unpinned", "no hash")

        def caller(paths):
            (path,) = [path for path in paths if "examples" in path]
            return zizmor_finding(path, 0, "excessive-permissions", "50%", "a\nb")

        status, output, commands = self.main(listed, caller)
        self.assertEqual(status, 1)
        self.assertEqual(
            output.splitlines(),
            [
                f"::error file=docs/guide.md,line=3::{lint_snippets.MISPLACED}",
                "::error file=README.md,line=66::unpinned-uses: unpinned (no hash)",
                (
                    "::error file=examples/app/.github/workflows/ci.yml,line=1::"
                    "excessive-permissions: 50%25 (a%0Ab)"
                ),
            ],
        )
        actionlint, zizmor = commands
        # Six snippets: five from the README and the caller.
        self.assertEqual(len(actionlint), 3 + 6)
        self.assertIn("--no-ignores", zizmor)

    def test_a_zizmor_failure_still_reports_the_other_problems(self):
        def syntax(paths):
            (path,) = [path for path in paths if path.endswith("-L37.yml")]
            return actionlint_error(path, 6, "syntax-check", "bad")

        status, output, _ = self.main(actionlint=[syntax], zizmor_error="failed")
        self.assertEqual(status, "failed")
        # Without zizmor's findings, the marker's unpinned-uses is not missing.
        self.assertEqual(
            output.splitlines(),
            [
                f"::error file=docs/guide.md,line=3::{lint_snippets.MISPLACED}",
                "::error file=README.md,line=39::syntax-check: bad",
            ],
        )

    def test_no_findings_pass(self):
        (self.root / "docs/guide.md").write_text("Text.\n", encoding="utf-8")
        status, output, _ = self.main()
        self.assertEqual(status, 0, output)
        self.assertIn("found nothing in 6 workflow examples", output)


class RunTests(unittest.TestCase):
    def test_a_missing_or_failing_linter_stops_the_check(self):
        with self.assertRaisesRegex(SystemExit, "not-a-linter is not installed"):
            lint_snippets.run(["not-a-linter"], ".")
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('bad'); sys.exit(2)",
        ]
        with self.assertRaisesRegex(SystemExit, "failed with exit status 2:\nbad"):
            lint_snippets.run(command, ".", 1)
        # The exit status that means findings is not a failure.
        command[2] = "import sys; print('[]'); sys.exit(1)"
        self.assertEqual(lint_snippets.run(command, ".", 1).strip(), "[]")


@unittest.skipUnless(yaml, "needs PyYAML")
class WorkflowLintTests(unittest.TestCase):
    def test_workflow_lint_runs_the_script_with_the_token_for_online_audits(self):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/workflow-lint.yml").read_text(encoding="utf-8")
        )
        (job,) = workflow["jobs"].values()
        steps = {step.get("name"): step for step in job["steps"]}
        step = steps["Lint documentation snippets"]
        self.assertEqual(step["run"], "python .github/scripts/lint_snippets.py")
        self.assertEqual(step["env"], {"GH_TOKEN": "${{ github.token }}"})
        # The linters come from the step that installs requirements-lint.txt.
        names = list(steps)
        self.assertLess(names.index("Install linters"), names.index(step["name"]))
