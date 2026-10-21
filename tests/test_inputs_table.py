"""Keep the generated columns of the README inputs tables in step with their sources."""

import contextlib
import importlib.util
import io
import re
import shutil
import subprocess
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
SCRIPT = ROOT / ".github/scripts/inputs_table.py"
SOURCES = ("README.md", "CHANGELOG.md", ".github/workflows/python-ci.yml")
COLUMNS = ("Type", "Default", "Added in")
NEW_INPUT = (
    "      new-thing:\n"
    '        description: "A new input"\n'
    "        type: boolean\n"
    "        default: false\n"
)


def read(path):
    return path.read_text(encoding="utf-8")


def rows(readme):
    """Return the input names that rows of the README inputs tables document."""
    inputs = readme.split("\n## Inputs\n", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| `([a-z-]+)` \|.*$", inputs, re.MULTILINE)


@unittest.skipUnless(yaml, "needs PyYAML")
class InputsTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("inputs_table", SCRIPT)
        assert spec and spec.loader
        cls.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.script)
        cls.readme, cls.changelog, cls.workflow = (
            read(ROOT / name) for name in SOURCES
        )

    def check(self, readme=None, workflow=None, changelog=None):
        """Return the cells the check would rewrite and the problems it reports."""
        expected = self.script.expected_cells(
            workflow or self.workflow, changelog or self.changelog
        )
        return self.script.update(readme or self.readme, expected)[1:]

    def add_new_thing(self, bullet):
        """Return the workflow, CHANGELOG and README with a new-thing input added.

        The README row says 1.0.0, and the bullet starts the Unreleased Added list.
        """
        workflow = self.workflow.replace(
            "    inputs:\n", "    inputs:\n" + NEW_INPUT, 1
        )
        unreleased, rest = self.changelog.split("### Added\n\n", 1)
        changelog = f"{unreleased}### Added\n\n{bullet}\n{rest}"
        (line,) = re.findall(r"^\| `call-name` \|.*\n", self.readme, re.MULTILINE)
        row = "| `new-thing` | boolean | `false` | 1.0.0 | A new input. |\n"
        return workflow, changelog, self.readme.replace(line, line + row)

    def run_main(self, root, *args):
        """Run the script on a copy of the sources in root; return code and output."""
        output = io.StringIO()
        with (
            unittest.mock.patch.object(self.script, "ROOT", root),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            return self.script.main(list(args)), output.getvalue()

    def copy_sources(self):
        """Return a temporary root holding copies of the script's sources."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name in SOURCES:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, root / name)
        return root

    def edit_row(self, readme, name, column, value):
        """Return the README with one cell of an input's row replaced."""
        (line,) = re.findall(rf"^\| `{name}` \|.*$", readme, re.MULTILINE)
        cells = self.script.split_row(line)
        cells[("Input", *COLUMNS).index(column)] = value
        return readme.replace(line, "| " + " | ".join(cells) + " |")

    def test_the_readme_tables_match_the_workflow_and_changelog(self):
        # The check mode CONTRIBUTING describes, run on this repository's own files.
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_generated_cell_that_differs_fails(self):
        self.assertEqual(self.check(), ([], []))
        self.assertEqual(
            sorted(rows(self.readme)), sorted(self.script.declared(self.workflow))
        )
        for name in rows(self.readme):
            for column in COLUMNS:
                with self.subTest(input=name, column=column):
                    readme = self.edit_row(self.readme, name, column, "`stale`")
                    changes, problems = self.check(readme)
                    self.assertEqual(problems, [])
                    self.assertEqual(len(changes), 1)
                    self.assertIn(f"{column} of {name} is `stale`", changes[0])

    def test_a_workflow_change_without_the_readme_fails(self):
        workflow = re.sub(
            r"(?m)^(      uv-version:\n(?:        .*\n)*?        default: ).*$",
            r'\g<1>"0.0.1"',
            self.workflow,
        )
        workflow = re.sub(
            r"(?m)^(      max-parallel:\n(?:        .*\n)*?        type: ).*$",
            r"\g<1>string",
            workflow,
        )
        changes, problems = self.check(workflow=workflow)
        self.assertEqual(problems, [])
        type_change, default_change = changes
        self.assertEqual(
            type_change, "README.md: Type of max-parallel is number, not string"
        )
        self.assertRegex(
            default_change, r"^README.md: Default of uv-version is .*, not `0\.0\.1`$"
        )

    def test_a_changelog_introduction_without_the_readme_fails(self):
        workflow, changelog, readme = self.add_new_thing(
            "- `new-thing` input to do new things."
        )
        changes, problems = self.check(readme, workflow, changelog)
        self.assertEqual(problems, [])
        self.assertEqual(
            changes, ["README.md: Added in of new-thing is 1.0.0, not Unreleased"]
        )

    def test_added_in_names_the_section_whose_added_list_introduces_the_input(self):
        changelog = (
            "# Changelog\n\n## [Unreleased]\n\n### Breaking changes\n\n### Added\n\n"
            "- `fresh` input to do something new.\n"
            "- `test-runner: auto`, a new value of an existing input.\n\n"
            "### Changed\n\n- `max-parallel` input now does more.\n\n"
            "## [1.2.0] - 2026-11-01\n\n### Added\n\n"
            "- A feature first, and the `late` input later in the bullet.\n"
            "- `pair` inputs, two at once.\n"
            "- `newer` input to do more,\n  over two lines.\n\n"
            "### Fixed\n\n- `fixed` input works.\n\n"
            "## [1.0.0]\n\nFirst tagged release.\n\n### Added\n\n- Everything else.\n"
        )
        self.assertEqual(
            self.script.introductions(changelog),
            {"fresh": "Unreleased", "newer": "1.2.0"},
        )
        workflow = (
            "on:\n  workflow_call:\n    inputs:\n"
            '      fresh:\n        type: string\n        default: ""\n'
            "      newer:\n        type: boolean\n        default: true\n"
            "      max-parallel:\n        type: number\n        default: 3\n"
            '      pytest-args:\n        type: string\n        default: "a|`b`"\n'
        )
        self.assertEqual(
            self.script.expected_cells(workflow, changelog),
            {
                "fresh": ("string", '`""`', "Unreleased"),
                "newer": ("boolean", "`true`", "1.2.0"),
                "max-parallel": ("number", "`3`", "1.0.0"),
                "pytest-args": ("string", "`` a\\|`b` ``", "1.0.0"),
            },
        )

    def test_sources_that_disagree_are_rejected(self):
        twice = self.changelog.replace(
            "### Added\n\n", "### Added\n\n- `call-name` input again.\n", 1
        )
        unknown = self.changelog.replace(
            "### Added\n\n", "### Added\n\n- `no-such-thing` input.\n", 1
        )
        again = self.changelog.replace(
            "### Added\n\n", "### Added\n\n- `test-wheel` input again.\n", 1
        )
        no_default = re.sub(
            r"(?m)^(      uv-version:\n(?:        .*\n)*?)        default: .*\n",
            r"\1",
            self.workflow,
        )
        cases = {
            "under both Unreleased and 1.1.0": {"changelog": twice},
            "declares no such input": {"changelog": unknown},
            "already declared it at v1.0.0": {"changelog": again},
            "needs a type and a default": {"workflow": no_default},
        }
        for message, sources in cases.items():
            with self.subTest(message):
                with self.assertRaises(SystemExit) as raised:
                    self.check(**sources)
                self.assertIn(message, str(raised.exception))

    def test_a_new_input_needs_an_added_bullet_that_introduces_it(self):
        # Only the inputs 1.0.0 declared date from 1.0.0 without a bullet, so a new
        # input that no bullet introduces as '`<name>` input' has no release to show.
        for bullet in (
            None,
            "- A `new-thing` input to do new things.",
            "- `new-thing` and `other-thing` inputs to do new things.",
            "- `new-thing`, to do new things.",
        ):
            workflow, changelog, readme = self.add_new_thing(bullet or "")
            with self.subTest(bullet=bullet):
                with self.assertRaises(SystemExit) as raised:
                    self.check(readme, workflow, changelog if bullet else None)
                self.assertEqual(
                    str(raised.exception),
                    ".github/workflows/python-ci.yml declares the new-thing input, "
                    "but no bullet in an Added subsection of CHANGELOG.md starts "
                    "with '`new-thing` input'; add one under Unreleased",
                )

    def test_rows_must_match_the_declared_inputs(self):
        (line,) = re.findall(r"^\| `call-name` \|.*\n", self.readme, re.MULTILINE)
        cases = {
            "no row for the call-name input": self.readme.replace(line, ""),
            "documents call-name twice": self.readme.replace(line, line + line),
            "documents renamed, which": self.readme.replace(
                line, line + line.replace("call-name", "renamed")
            ),
            "is not an input row": self.readme.replace(line, line + "| a | b |\n"),
        }
        for message, readme in cases.items():
            with self.subTest(message):
                problems = self.check(readme)[1]
                self.assertTrue(any(message in p for p in problems), problems)
        # A table with other columns is not an inputs table.
        old = self.readme.replace("| Added in |", "|")
        self.assertIn(
            "README.md has no row for the call-name input in a table headed "
            "'| Input | Type | Default | Added in | Description |'",
            self.check(old)[1],
        )

    def test_write_rewrites_only_the_generated_cells(self):
        root = self.copy_sources()
        stale = self.readme
        for name in rows(stale):
            for column in COLUMNS:
                stale = self.edit_row(stale, name, column, "")
        readme = root / "README.md"
        readme.write_text(stale, encoding="utf-8")
        code, output = self.run_main(root)
        self.assertEqual(code, 1)
        self.assertIn("is empty, not `256`", output)
        self.assertIn("inputs_table.py --write", output)
        self.assertEqual(read(readme), stale)
        self.assertEqual(self.run_main(root, "--write")[0], 0)
        self.assertEqual(read(readme), self.readme)
        self.assertEqual(self.run_main(root)[0], 0)
        # Rows it cannot write fail, and nothing is written.
        readme.write_text(stale.replace("`call-name`", "`renamed`"), "utf-8")
        self.assertEqual(self.run_main(root, "--write")[0], 1)
        self.assertIn("`renamed`", read(readme))

    def test_write_keeps_the_line_endings(self):
        # A Windows checkout may have either, and one cell must not rewrite them all.
        root = self.copy_sources()
        stale = self.edit_row(self.readme, "uv-version", "Default", "`0.0.1`")
        for ending in ("\n", "\r\n"):
            with self.subTest(ending=ending):
                readme = root / "README.md"
                readme.write_bytes(stale.replace("\n", ending).encode("utf-8"))
                code, output = self.run_main(root, "--write")
                self.assertEqual(code, 0, output)
                self.assertIn("Updated 1 cells", output)
                self.assertEqual(
                    readme.read_bytes(),
                    self.readme.replace("\n", ending).encode("utf-8"),
                )


if __name__ == "__main__":
    unittest.main()
